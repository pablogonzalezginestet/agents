# coding=utf-8
# Copyright 2018 The TF-Agents Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A DDPG Agent.

Implements the Deep Deterministic Policy Gradient (DDPG) algorithm from
"Continuous control with deep reinforcement learning" - Lilicrap et al.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf


from tf_agents.agents import tf_agent
from tf_agents.agents.ddpg import networks
from tf_agents.environments import trajectory
from tf_agents.policies import actor_policy
from tf_agents.policies import ou_noise_policy
import tf_agents.utils.common as common_utils
import gin

nest = tf.contrib.framework.nest


@gin.configurable
class DdpgAgent(tf_agent.BaseV2):
  """A DDPG Agent."""

  ACTOR_NET_SCOPE = 'actor_net'
  TARGET_ACTOR_NET_SCOPE = 'target_actor_net'
  CRITIC_NET_SCOPE = 'critic_net'
  TARGET_CRITIC_NET_SCOPE = 'target_critic_net'

  def __init__(self,
               time_step_spec,
               action_spec,
               # TODO(kbanoop): rename to actor_network.
               actor_net=networks.actor_network,
               critic_net=networks.critic_network,
               actor_optimizer=None,
               critic_optimizer=None,
               ou_stddev=1.0,
               ou_damping=1.0,
               target_update_tau=1.0,
               target_update_period=1,
               dqda_clipping=None,
               td_errors_loss_fn=None,
               gamma=1.0,
               reward_scale_factor=1.0,
               gradient_clipping=None,
               debug_summaries=False,
               summarize_grads_and_vars=False):
    """Creates a DDPG Agent.

    Args:
      time_step_spec: A `TimeStep` spec of the expected time_steps.
      action_spec: A nest of BoundedTensorSpec representing the actions.
      actor_net: A function actor_net(observation, action_spec) that returns
        the actions for each observation.
      critic_net: A function critic_net(observations, actions) that returns
        the q_values for each observation and action.
      actor_optimizer: The optimizer to use for the actor network.
      critic_optimizer: The optimizer to use for the critic network.
      ou_stddev: Standard deviation for the Ornstein-Uhlenbeck (OU) noise added
        in the default collect policy.
      ou_damping: Damping factor for the OU noise added in the default collect
        policy.
      target_update_tau: Factor for soft update of the target networks.
      target_update_period: Period for soft update of the target networks.
      dqda_clipping: when computing the actor loss, clips the gradient dqda
        element-wise between [-dqda_clipping, dqda_clipping]. Does not perform
        clipping if dqda_clipping == 0.
      td_errors_loss_fn:  A function for computing the TD errors loss. If None,
        a default value of tf.losses.huber_loss is used.
      gamma: A discount factor for future rewards.
      reward_scale_factor: Multiplicative scale for the reward.
      gradient_clipping: Norm length to clip gradients.
      debug_summaries: A bool to gather debug summaries.
      summarize_grads_and_vars: If True, gradient and network variable summaries
        will be written during training.
    """
    self._actor_net = tf.make_template(
        self.ACTOR_NET_SCOPE, actor_net, create_scope_now_=True)
    self._target_actor_net = tf.make_template(
        self.TARGET_ACTOR_NET_SCOPE, actor_net, create_scope_now_=True)

    self._critic_net = tf.make_template(
        self.CRITIC_NET_SCOPE, critic_net, create_scope_now_=True)
    self._target_critic_net = tf.make_template(
        self.TARGET_CRITIC_NET_SCOPE, critic_net, create_scope_now_=True)

    self._actor_optimizer = actor_optimizer
    self._critic_optimizer = critic_optimizer

    self._ou_stddev = ou_stddev
    self._ou_damping = ou_damping
    self._target_update_tau = target_update_tau
    self._target_update_period = target_update_period
    self._dqda_clipping = dqda_clipping
    self._td_errors_loss_fn = td_errors_loss_fn or tf.losses.huber_loss
    self._gamma = gamma
    self._reward_scale_factor = reward_scale_factor
    self._gradient_clipping = gradient_clipping

    policy = actor_policy.ActorPolicy(
        time_step_spec=time_step_spec, action_spec=action_spec,
        actor_network=self._actor_net, clip=True)

    collect_policy = actor_policy.ActorPolicy(
        time_step_spec=time_step_spec, action_spec=action_spec,
        actor_network=self._actor_net, clip=False)
    collect_policy = ou_noise_policy.OUNoisePolicy(
        collect_policy,
        ou_stddev=self._ou_stddev,
        ou_damping=self._ou_damping,
        clip=True)

    super(DdpgAgent, self).__init__(
        time_step_spec,
        action_spec,
        policy,
        collect_policy,
        train_sequence_length=2,  # TODO(kbanoop): update after merging RNNs.
        debug_summaries=debug_summaries,
        summarize_grads_and_vars=summarize_grads_and_vars)

  def _initialize(self):
    return self._update_targets(1.0, 1)

  def _update_targets(self, tau=1.0, period=1):
    """Performs a soft update of the target network parameters.

    For each weight w_s in the original network, and its corresponding
    weight w_t in the target network, a soft update is:
    w_t = (1- tau) x w_t + tau x ws

    Args:
      tau: A float scalar in [0, 1]. Default `tau=1.0` means hard update.
      period: Step interval at which the target networks are updated.
    Returns:
      An operation that performs a soft update of the target network parameters.
    """
    with tf.name_scope('update_targets'):
      def update():
        critic_update = common_utils.soft_variables_update(
            self._critic_net.global_variables,
            self._target_critic_net.global_variables, tau)
        actor_update = common_utils.soft_variables_update(
            self._actor_net.global_variables,
            self._target_actor_net.global_variables, tau)
        return tf.group(critic_update, actor_update)

      return common_utils.periodically(update, period,
                                       'periodic_update_targets')

  def _experience_to_transitions(self, experience):
    transitions = trajectory.to_transition(experience)

    # Remove time dim since we are not using a recurrent network.
    transitions = nest.map_structure(lambda x: tf.squeeze(x, [1]), transitions)

    time_steps, policy_steps, next_time_steps = transitions
    actions = policy_steps.action
    return time_steps, actions, next_time_steps

  def _train(self, experience, train_step_counter=None):
    time_steps, actions, next_time_steps = self._experience_to_transitions(
        experience)

    # TODO(kbanoop): Compute and apply a loss mask.
    critic_loss = self.critic_loss(time_steps, actions, next_time_steps)
    actor_loss = self.actor_loss(time_steps)

    def clip_and_summarize_gradients(grads_and_vars):
      """Clips gradients, and summarizes gradients and variables."""
      if self._gradient_clipping is not None:
        grads_and_vars = tf.contrib.training.clip_gradient_norms_fn(
            self._gradient_clipping)(grads_and_vars)

      if self._summarize_grads_and_vars:
        # TODO(kbanoop): Move gradient summaries to train_op after we switch to
        # eager train op, and move variable summaries to critic_loss.
        for grad, var in grads_and_vars:
          with tf.name_scope('Gradients/'):
            if grad is not None:
              tf.contrib.summary.histogram(grad.op.name, grad)
          with tf.name_scope('Variables/'):
            if var is not None:
              tf.contrib.summary.histogram(var.op.name, var)
      return grads_and_vars

    critic_train_op = tf.contrib.training.create_train_op(
        critic_loss,
        self._critic_optimizer,
        global_step=train_step_counter,
        transform_grads_fn=clip_and_summarize_gradients,
        variables_to_train=self._critic_net.trainable_variables,
    )

    actor_train_op = tf.contrib.training.create_train_op(
        actor_loss,
        self._actor_optimizer,
        global_step=None,
        transform_grads_fn=clip_and_summarize_gradients,
        variables_to_train=self._actor_net.trainable_variables,
    )

    with tf.control_dependencies([critic_train_op, actor_train_op]):
      update_targets_op = self._update_targets(self._target_update_tau,
                                               self._target_update_period)

    with tf.control_dependencies([update_targets_op]):
      total_loss = actor_loss + critic_loss

    # TODO(kbanoop): Compute per element TD loss and return in loss_info.
    return tf_agent.LossInfo(total_loss, ())

  def critic_loss(self,
                  time_steps,
                  actions,
                  next_time_steps):
    """Computes the critic loss for DDPG training.

    Args:
      time_steps: A batch of timesteps.
      actions: A batch of actions.
      next_time_steps: A batch of next timesteps.
    Returns:
      critic_loss: A scalar critic loss.
    """
    with tf.name_scope('critic_loss'):
      target_actions = self._target_actor_net(next_time_steps,
                                              self.action_spec())
      target_q_values = self._target_critic_net(next_time_steps,
                                                target_actions)
      td_targets = tf.stop_gradient(
          self._reward_scale_factor * next_time_steps.reward +
          self._gamma * next_time_steps.discount * target_q_values)

      q_values = self._critic_net(time_steps, actions)
      critic_loss = self._td_errors_loss_fn(td_targets, q_values)
      with tf.name_scope('Losses/'):
        tf.contrib.summary.scalar('critic_loss', critic_loss)

      if self._debug_summaries:
        td_errors = td_targets - q_values
        common_utils.generate_tensor_summaries('td_errors', td_errors)
        common_utils.generate_tensor_summaries('td_targets', td_targets)
        common_utils.generate_tensor_summaries('q_values', q_values)

      return critic_loss

  def actor_loss(self, time_steps):
    """Computes the actor_loss for DDPG training.

    Args:
      time_steps: A batch of timesteps.
      # TODO(kbanoop): Add an action norm regularizer.
    Returns:
      actor_loss: A scalar actor loss.
    """
    with tf.name_scope('actor_loss'):
      actions = self._actor_net(time_steps, self.action_spec())

      q_values = self._critic_net(time_steps, actions)
      actions = nest.flatten(actions)
      dqda = tf.gradients([q_values], actions)
      actor_losses = []
      for dqda, action in zip(dqda, actions):
        if self._dqda_clipping is not None:
          dqda = tf.clip_by_value(dqda, -1 * self._dqda_clipping,
                                  self._dqda_clipping)
        actor_losses.append(
            tf.losses.mean_squared_error(
                tf.stop_gradient(dqda + action), action))
      actor_loss = tf.add_n(actor_losses)
      with tf.name_scope('Losses/'):
        tf.contrib.summary.scalar('actor_loss', actor_loss)

    return actor_loss
