from typing import Tuple

import jax
import jax.numpy as jnp

from common import Batch, InfoDict, Model, Params, PRNGKey
from common import expectile_loss as loss, get_deter, get_stoch

sg = lambda x: jax.lax.stop_gradient(x)


def update_actor_bc(actor: Model, batch: Batch) -> Tuple[Model, InfoDict]:

    def actor_loss_fn(actor_params: Params) -> Tuple[jnp.ndarray, InfoDict]:
        dist = actor.apply(
            {"params": actor_params}, batch.observations, 0.0, training=True
        )
        a = get_deter(dist)

        actor_loss = jnp.mean((a - batch.actions) ** 2)
        return actor_loss, {"actor_loss": actor_loss}

    new_actor, info = actor.apply_gradient(actor_loss_fn)
    return new_actor, info


def update_alpha(
    key: PRNGKey, log_probs: jnp.ndarray, sac_alpha: Model, target_entropy: float
) -> Tuple[Model, InfoDict]:
    log_probs = log_probs + target_entropy

    def alpha_loss_fn(alpha_params: Params) -> Tuple[jnp.ndarray, InfoDict]:
        log_alpha = sac_alpha.apply({"params": alpha_params})
        alpha_loss = -(log_alpha * log_probs).mean()
        return alpha_loss, {"alpha_loss": alpha_loss, "alpha": jnp.exp(log_alpha)}

    new_alpha, info = sac_alpha.apply_gradient(alpha_loss_fn)

    return new_alpha, info


def onestep_update_actor(
    key: PRNGKey,
    actor: Model,
    critic: Model,
    model: Model,
    batch: Batch,
    discount: float,
    H: int,
    expectile: float,
    num_repeat: int,
) -> Tuple[Model, InfoDict]:

    N = batch.observations.shape[0]
    Robs = (
        batch.observations[:, None, :]
        .repeat(repeats=num_repeat, axis=1)
        .reshape(N * num_repeat, -1)
    )
    Ra = get_deter(actor(Robs))

    def calculate_gae_foward(Robs, Ra, key0):
        states, rewards, actions, mask_weights, keys = [Robs], [], [Ra], [1.0], [key0]
        for i in range(H):
            rng1, rng2, rng3, key0 = jax.random.split(keys[-1], 4)
            keys.append(key0)
            s_next, rew, terminal, _ = model(rng1, states[i], actions[i])
            a_next = get_deter(actor(s_next))
            states.append(s_next)
            actions.append(a_next)
            rewards.append(rew)

        states = jnp.stack(states, axis=0)
        actions = jnp.stack(actions, axis=0)
        mask_weights = jnp.stack(mask_weights, axis=0)
        return states, actions, mask_weights

    keys = jax.random.split(key, N * num_repeat)
    vmap_foward = lambda func: jax.vmap(func, in_axes=0, out_axes=1)
    states0, actions0, mask_weights0 = vmap_foward(calculate_gae_foward)(Robs, Ra, keys)

    def actor_loss_fn(actor_params: Params) -> Tuple[jnp.ndarray, InfoDict]:
        dist = actor.apply({"params": actor_params}, states0)
        actions = get_deter(dist)
        actor_loss = -(mask_weights0 * critic(states0, actions)).mean()
        policy_std = dist.scale if hasattr(dist, "scale") else dist.distribution.scale

        return actor_loss, {
            "actor_loss": actor_loss,
            "policy_std": (policy_std * mask_weights0[:, :, None]).mean()
            / mask_weights0.mean(),
            "abs_actions": jnp.abs(actions0).mean(),
        }

    new_actor, info = actor.apply_gradient(actor_loss_fn)
    return new_actor, info


def DPG_lambda_update_actor(
    key: PRNGKey,
    actor: Model,
    critic: Model,
    model: Model,
    batch: Batch,
    discount: float,
    lamb: float,
    H: int,
    expectile: float,
    num_repeat: int,
) -> Tuple[Model, InfoDict]:

    N = batch.observations.shape[0]
    Robs = (
        batch.observations[:, None, :]
        .repeat(repeats=num_repeat, axis=1)
        .reshape(N * num_repeat, -1)
    )
    Ra = get_deter(actor(Robs))

    def calculate_gae_foward(Robs, Ra, key0):
        ## Generate imagined trajectory
        states, rewards, actions, mask_weights, keys = [Robs], [], [Ra], [1.0], [key0]
        q_rollout, q_values, ep_weights = [], [critic(Robs, Ra)], []
        for i in range(H):
            rng1, rng2, rng3, key0 = jax.random.split(keys[-1], 4)
            keys.append(key0)
            s_next, rew, terminal, _ = model(rng1, states[i], actions[i])
            a_next = get_deter(actor(s_next))
            states.append(s_next)
            actions.append(a_next)
            rewards.append(rew)
            mask_weights.append(mask_weights[i] * (1 - terminal))
            q_values.append(critic(s_next, a_next))

        ## Calculate lambda-returns
        q_rollout, lamb_weight = [q_values[-1]], 1.0
        for i in reversed(range(H)):
            q_next = (
                mask_weights[i] * rewards[i]
                + mask_weights[i + 1] * discount * q_rollout[-1]
            )
            next_value = (q_values[i] + lamb * lamb_weight * q_next) / (
                1 + lamb * lamb_weight
            )
            q_rollout.append(next_value)
            lamb_weight = 1.0 + lamb * lamb_weight
        q_rollout = list(reversed(q_rollout))

        ## Calculate asymmetric weights
        ep_weights = []
        for i in range(H):
            ep_weights.append(
                jnp.where(q_rollout[i] > q_values[i], expectile, 1 - expectile)
            )
        ep_weights.append(0.5)

        states = jnp.stack(states, axis=0)
        actions = jnp.stack(actions, axis=0)
        mask_weights = jnp.stack(mask_weights, axis=0)
        q_rollout = jnp.stack(q_rollout, axis=0)
        ep_weights = jnp.stack(ep_weights, axis=0)
        return states, actions, mask_weights, q_rollout, ep_weights

    keys = jax.random.split(key, N * num_repeat)
    vmap_foward = lambda func: jax.vmap(func, in_axes=0, out_axes=1)
    states0, actions0, mask_weights0, q_rollout, ep_weights = vmap_foward(
        calculate_gae_foward
    )(Robs, Ra, keys)

    def calculate_gae_backward(delta_a, Robs, Ra, key0):
        ## Generate imagined trajectory (identical with foward)
        states, rewards, actions, mask_weights, keys = (
            [Robs],
            [],
            [Ra + delta_a[0]],
            [1.0],
            [key0],
        )
        q_rollout, q_values, ep_weights = [], [critic(Robs, Ra + delta_a[0])], []
        for i in range(H):
            rng1, rng2, rng3, key0 = jax.random.split(keys[-1], 4)
            keys.append(key0)
            s_next, rew, terminal, _ = model(rng1, states[i], actions[i])
            a_next = get_deter(actor(s_next)) + delta_a[i + 1]
            states.append(s_next)
            actions.append(a_next)
            rewards.append(rew)
            mask_weights.append(mask_weights[i] * (1 - terminal))
            q_values.append(critic(s_next, a_next))

        ## Calculate lambda-returns
        q_rollout, lamb_weight = [q_values[-1]], 1.0
        for i in reversed(range(H)):
            q_next = (
                mask_weights[i] * rewards[i]
                + mask_weights[i + 1] * discount * q_rollout[-1]
            )
            next_value = (q_values[i] + lamb * lamb_weight * q_next) / (
                1 + lamb * lamb_weight
            )
            q_rollout.append(next_value)
            lamb_weight = 1.0 + lamb * lamb_weight
        q_rollout = list(reversed(q_rollout))

        return jnp.stack(q_rollout, axis=0)

    ## Calculate gradient of Q_t^{\lambda} w.r.t a_t
    delta_a = jnp.zeros_like(actions0)
    vmap_backward = lambda func: jax.vmap(func, in_axes=(1, 0, 0, 0), out_axes=1)
    grad_gae = vmap_backward(jax.jacrev(calculate_gae_backward))(
        delta_a, Robs, Ra, keys
    )
    grad_gae = jnp.stack([grad_gae[i, :, i] for i in range(H + 1)])

    def actor_loss_fn(actor_params: Params) -> Tuple[jnp.ndarray, InfoDict]:
        dist = actor.apply({"params": actor_params}, states0, 1.0, training=True)
        actions_grad = get_deter(dist)
        policy_std = dist.scale if hasattr(dist, "scale") else dist.distribution.scale

        ## Calculate gradient of Q_t^{\lambda} w.r.t parameter using deterministic policy gradient theorem (chain rule)
        actor_loss = (
            -(ep_weights[:, :, None] * grad_gae * actions_grad).mean(axis=1).sum()
        )
        return actor_loss, {
            "actor_loss": actor_loss,
            "q_rollout": q_rollout.mean(),
            "lambda_actor": lamb,
            "policy_std": (policy_std * mask_weights0[:, :, None]).mean()
            / mask_weights0.mean(),
            "adv_weights": (ep_weights * mask_weights0).mean() / mask_weights0.mean(),
            "abs_actions": jnp.abs(actions0).mean(),
        }

    new_actor, info = actor.apply_gradient(actor_loss_fn)
    return new_actor, info


def DPG_multistep_update_actor(
    key: PRNGKey,
    actor: Model,
    critic: Model,
    model: Model,
    batch: Batch,
    discount: float,
    H: int,
    expectile: float,
    num_repeat: int,
) -> Tuple[Model, InfoDict]:

    N = batch.observations.shape[0]
    Robs = (
        batch.observations[:, None, :]
        .repeat(repeats=num_repeat, axis=1)
        .reshape(N * num_repeat, -1)
    )
    Ra = get_deter(actor(Robs))

    def calculate_gae_foward(Robs, Ra, key0):
        ## Generate imagined trajectory (identical with foward)
        states, rewards, actions, mask_weights, keys = [Robs], [], [Ra], [1.0], [key0]
        q_rollout, q_values, ep_weights = [], [critic(Robs, Ra)], []
        for i in range(H):
            rng1, rng2, rng3, key0 = jax.random.split(keys[-1], 4)
            keys.append(key0)
            s_next, rew, terminal, _ = model(rng1, states[i], actions[i])
            a_next = get_deter(actor(s_next))
            states.append(s_next)
            actions.append(a_next)
            rewards.append(rew)
            mask_weights.append(mask_weights[i] * (1 - terminal))
            q_values.append(critic(s_next, a_next))

        ## Calculate multi-step returns
        q_rollout, lamb_weight = [q_values[-1]], 1.0
        for i in reversed(range(H)):
            q_next = (
                mask_weights[i] * rewards[i]
                + mask_weights[i + 1] * discount * q_rollout[-1]
            )
            q_rollout.append(q_next)
        q_rollout = list(reversed(q_rollout))

        ## Calculate asymmetric weights
        ep_weights = []
        for i in range(H):
            ep_weights.append(
                jnp.where(q_rollout[i] > q_values[i], expectile, 1 - expectile)
            )
        ep_weights.append(0.5)

        states = jnp.stack(states, axis=0)
        actions = jnp.stack(actions, axis=0)
        mask_weights = jnp.stack(mask_weights, axis=0)
        q_rollout = jnp.stack(q_rollout, axis=0)
        ep_weights = jnp.stack(ep_weights, axis=0)
        return states, actions, mask_weights, q_rollout, ep_weights

    keys = jax.random.split(key, N * num_repeat)
    vmap_foward = lambda func: jax.vmap(func, in_axes=0, out_axes=1)
    states0, actions0, mask_weights0, q_rollout, ep_weights = vmap_foward(
        calculate_gae_foward
    )(Robs, Ra, keys)

    def calculate_gae_backward(delta_a, Robs, Ra, key0):
        ## Generate imagined trajectory (identical with foward)
        states, rewards, actions, mask_weights, keys = (
            [Robs],
            [],
            [Ra + delta_a[0]],
            [1.0],
            [key0],
        )
        q_rollout, q_values, ep_weights = [], [critic(Robs, Ra + delta_a[0])], []
        for i in range(H):
            rng1, rng2, rng3, key0 = jax.random.split(keys[-1], 4)
            keys.append(key0)
            s_next, rew, terminal, _ = model(rng1, states[i], actions[i])
            a_next = get_deter(actor(s_next)) + delta_a[i + 1]
            states.append(s_next)
            actions.append(a_next)
            rewards.append(rew)
            mask_weights.append(mask_weights[i] * (1 - terminal))
            q_values.append(critic(s_next, a_next))

        ## Calculate multi-step returns
        q_rollout, lamb_weight = [q_values[-1]], 1.0
        for i in reversed(range(H)):
            q_next = (
                mask_weights[i] * rewards[i]
                + mask_weights[i + 1] * discount * q_rollout[-1]
            )
            q_rollout.append(q_next)
        q_rollout = list(reversed(q_rollout))

        return jnp.stack(q_rollout, axis=0)

    ## Calculate gradient of Q_t^H w.r.t a_t
    delta_a = jnp.zeros_like(actions0)
    vmap_backward = lambda func: jax.vmap(func, in_axes=(1, 0, 0, 0), out_axes=1)
    grad_gae = vmap_backward(jax.jacrev(calculate_gae_backward))(
        delta_a, Robs, Ra, keys
    )
    grad_gae = jnp.stack([grad_gae[i, :, i] for i in range(H + 1)])

    def actor_loss_fn(actor_params: Params) -> Tuple[jnp.ndarray, InfoDict]:
        dist = actor.apply({"params": actor_params}, states0, 1.0, training=True)
        actions_grad = get_deter(dist)
        policy_std = dist.scale if hasattr(dist, "scale") else dist.distribution.scale
        ## Calculate gradient of Q_t^H w.r.t parameter using deterministic policy gradient theorem (chain rule)
        actor_loss = (
            -(ep_weights[:, :, None] * grad_gae * actions_grad).mean(axis=1).sum()
        )

        return actor_loss, {
            "actor_loss": actor_loss,
            "q_rollout": q_rollout.mean(),
            "policy_std": (policy_std * mask_weights0[:, :, None]).mean()
            / mask_weights0.mean(),
            "adv_weights": (ep_weights * mask_weights0).mean() / mask_weights0.mean(),
            "abs_actions": jnp.abs(actions0).mean(),
        }

    new_actor, info = actor.apply_gradient(actor_loss_fn)
    return new_actor, info
