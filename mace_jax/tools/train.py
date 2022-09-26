import dataclasses
import logging
import time
from typing import Any, Callable, Dict, Optional, Tuple

import jax
import jax.numpy as jnp
import jraph
import numpy as np
import optax
import torch
from torch.optim.swa_utils import SWALR, AveragedModel
from torch.utils.data import DataLoader
from torch_ema import ExponentialMovingAverage

from .checkpoint import CheckpointHandler, CheckpointState
from .torch_tools import to_numpy
from .utils import (
    MetricsLogger,
    compute_mae,
    compute_q95,
    compute_rel_mae,
    compute_rel_rmse,
    compute_rmse,
)
from .jax_tools import get_batched_padded_graph_tuples


@dataclasses.dataclass
class SWAContainer:
    model: AveragedModel
    scheduler: SWALR
    start: int
    loss_fn: torch.nn.Module


@dataclasses.dataclass
class ExponentialMovingAverage:
    params: Dict[str, Any]
    decay: float


jax.tree_util.register_pytree_node(
    ExponentialMovingAverage,
    lambda x: ((x.params, x.decay), None),
    lambda _, children: ExponentialMovingAverage(prams=children[0], decay=children[1]),
)


def train(
    model: Callable,
    params: Dict[str, Any],
    loss_fn: Any,
    train_loader: DataLoader,
    valid_loader: DataLoader,
    gradient_transform: Any,
    optimizer_state: Dict[str, Any],
    start_epoch: int,
    max_num_epochs: int,
    patience: int,
    checkpoint_handler: CheckpointHandler,
    logger: MetricsLogger,
    eval_interval: int,
    device: torch.device,
    log_errors: str,
    swa: Optional[SWAContainer] = None,
    ema: Optional[ExponentialMovingAverage] = None,
    max_grad_norm: Optional[float] = 10.0,
):
    lowest_loss = np.inf
    patience_counter = 0
    num_updates = 0
    swa_start = True

    if max_grad_norm is not None:
        logging.info(f"Using gradient clipping with tolerance={max_grad_norm:.3f}")
    logging.info("Started training")

    @jax.jit
    def update_fn(
        params, optimizer_state, ema, num_updates: int, graph: jraph.GraphsTuple
    ) -> Tuple[float, Any, Any]:
        # graph is assumed to be padded by jraph.pad_with_graphs
        mask = jraph.get_graph_padding_mask(graph)  # [n_graphs,]
        loss, grad = jax.value_and_grad(
            lambda params: jnp.mean(loss_fn(graph, model(params, graph)) * mask)
        )(params)
        updates, optimizer_state = gradient_transform.update(grad, optimizer_state)
        params = optax.apply_updates(params, updates)
        if ema is not None:
            decay = min(ema.decay, (1 + num_updates) / (10 + num_updates))
            ema = ExponentialMovingAverage(
                params=jax.tree_util.tree_map(
                    lambda x, y: x * decay + y * (1 - decay), ema.params, params
                ),
                decay=ema.decay,
            )
        return loss, params, optimizer_state, ema

    for epoch in range(start_epoch, max_num_epochs):
        # Train
        for batch in train_loader:
            graph = get_batched_padded_graph_tuples(batch)
            num_updates += 1
            start_time = time.time()
            loss, params, optimizer_state, ema = update_fn(
                params, optimizer_state, ema, num_updates, graph
            )
            opt_metrics = {
                "loss": to_numpy(loss),
                "time": time.time() - start_time,
            }

            opt_metrics["mode"] = "opt"
            opt_metrics["epoch"] = epoch
            logger.log(opt_metrics)

        # Validate
        if epoch % eval_interval == 0:
            valid_loss, eval_metrics = evaluate(
                model=model,
                params=params if ema is not None else ema.params,
                loss_fn=loss_fn,
                data_loader=valid_loader,
                device=device,
            )
            eval_metrics["mode"] = "eval"
            eval_metrics["epoch"] = epoch
            logger.log(eval_metrics)
            if log_errors == "PerAtomRMSE":
                error_e = eval_metrics["rmse_e_per_atom"] * 1e3
                error_f = eval_metrics["rmse_f"] * 1e3
                logging.info(
                    f"Epoch {epoch}: loss={valid_loss:.4f}, RMSE_E_per_atom={error_e:.1f} meV, RMSE_F={error_f:.1f} meV / A"
                )
            elif log_errors == "TotalRMSE":
                error_e = eval_metrics["rmse_e"] * 1e3
                error_f = eval_metrics["rmse_f"] * 1e3
                logging.info(
                    f"Epoch {epoch}: loss={valid_loss:.4f}, RMSE_E={error_e:.1f} meV, RMSE_F={error_f:.1f} meV / A"
                )
            elif log_errors == "PerAtomMAE":
                error_e = eval_metrics["mae_e_per_atom"] * 1e3
                error_f = eval_metrics["mae_f"] * 1e3
                logging.info(
                    f"Epoch {epoch}: loss={valid_loss:.4f}, MAE_E_per_atom={error_e:.1f} meV, MAE_F={error_f:.1f} meV / A"
                )
            elif log_errors == "TotalMAE":
                error_e = eval_metrics["mae_e"] * 1e3
                error_f = eval_metrics["mae_f"] * 1e3
                logging.info(
                    f"Epoch {epoch}: loss={valid_loss:.4f}, MAE_E={error_e:.1f} meV, MAE_F={error_f:.1f} meV / A"
                )
            if valid_loss >= lowest_loss:
                patience_counter += 1
                if patience_counter >= patience:
                    logging.info(
                        f"Stopping optimization after {patience_counter} epochs without improvement"
                    )
                    break
            else:
                lowest_loss = valid_loss
                patience_counter = 0
                if ema is not None:
                    with ema.average_parameters():
                        checkpoint_handler.save(
                            state=CheckpointState(model, optimizer_state),
                            epochs=epoch,
                        )
                else:
                    checkpoint_handler.save(
                        state=CheckpointState(model, optimizer_state),
                        epochs=epoch,
                    )

        # LR scheduler and SWA update
        if swa is None or epoch < swa.start:
            pass
        else:
            if swa_start:
                logging.info("Changing loss based on SWA")
                swa_start = False
            loss_fn = swa.loss_fn
            swa.model.update_parameters(model)
            swa.scheduler.step()

    logging.info("Training complete")


def evaluate(
    model: Callable,
    params: Any,
    loss_fn: Any,
    data_loader: DataLoader,
) -> Tuple[float, Dict[str, Any]]:
    total_loss = 0.0
    delta_es_list = []
    delta_es_per_atom_list = []
    delta_fs_list = []
    fs_list = []

    start_time = time.time()
    for batch in data_loader:
        graph = get_batched_padded_graph_tuples(batch)
        output = model(params, graph)

        loss = loss_fn(graph=graph, pred=output)
        total_loss += float(loss)

        delta_es_list.append(batch.energy - output["energy"])
        delta_es_per_atom_list.append(
            (batch.energy - output["energy"]) / (batch.ptr[1:] - batch.ptr[:-1])
        )
        delta_fs_list.append(batch.forces - output["forces"])
        fs_list.append(batch.forces)

    avg_loss = total_loss / len(data_loader)

    delta_es = np.concatenate(delta_es_list, axis=0)
    delta_es_per_atom = np.concatenate(delta_es_per_atom_list, axis=0)
    delta_fs = np.concatenate(delta_fs_list, axis=0)
    fs = np.concatenate(fs_list, axis=0)

    aux = {
        "loss": avg_loss,
        # Mean absolute error
        "mae_e": compute_mae(delta_es),
        "mae_e_per_atom": compute_mae(delta_es_per_atom),
        "mae_f": compute_mae(delta_fs),
        "rel_mae_f": compute_rel_mae(delta_fs, fs),
        # Root-mean-square error
        "rmse_e": compute_rmse(delta_es),
        "rmse_e_per_atom": compute_rmse(delta_es_per_atom),
        "rmse_f": compute_rmse(delta_fs),
        "rel_rmse_f": compute_rel_rmse(delta_fs, fs),
        # Q_95
        "q95_e": compute_q95(delta_es),
        "q95_f": compute_q95(delta_fs),
        # Time
        "time": time.time() - start_time,
    }

    return avg_loss, aux
