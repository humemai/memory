"""DQN Agent for the RoomEnv2 environment."""
import datetime
import os
import random
import shutil
from copy import deepcopy
from typing import Dict, List, Tuple

import gymnasium as gym
import matplotlib.pyplot as plt
import numpy as np
import torch
import torch.nn.functional as F
import torch.optim as optim
from IPython.display import clear_output
from tqdm.auto import tqdm, trange

from explicit_memory.memory import (EpisodicMemory, MemorySystems,
                                    SemanticMemory, ShortMemory)
from explicit_memory.nn import LSTM
from explicit_memory.policy import (answer_question, encode_observation,
                                    explore, manage_memory)
from explicit_memory.utils import (ReplayBuffer, argmax,
                                   dqn_target_hard_update, plot_dqn,
                                   save_dqn_results, save_dqn_validation,
                                   select_dqn_action, update_dqn_model,
                                   write_yaml)

from .dqn import DQNAgent


class DQNMMAgent(DQNAgent):
    """DQN Agent interacting with environment.

    Based on https://github.com/Curt-Park/rainbow-is-all-you-need/
    """

    def __init__(
        self,
        env_str: str = "room_env:RoomEnv-v2",
        num_iterations: int = 1000,
        replay_buffer_size: int = 102400,
        warm_start: int = 102400,
        batch_size: int = 1024,
        target_update_rate: int = 10,
        epsilon_decay_until: float = 2048,
        max_epsilon: float = 1.0,
        min_epsilon: float = 0.1,
        gamma: float = 0.65,
        capacity: dict = {
            "episodic": 16,
            "episodic_agent": 0,
            "semantic": 16,
            "semantic_map": 0,
            "short": 1,
        },
        pretrain_semantic: bool = None,
        nn_params: dict = {
            "hidden_size": 64,
            "num_layers": 2,
            "embedding_dim": 32,
            "v1_params": None,
            "v2_params": {},
            "memory_of_interest": ["episodic", "semantic", "short"],
        },
        run_test: bool = True,
        num_samples_for_results: int = 10,
        plotting_interval: int = 10,
        train_seed: int = 5,
        test_seed: int = 0,
        device: str = "cpu",
        qa_policy: str = "episodic_semantic",
        explore_policy: str = "avoid_walls",
        env_config: dict = {
            "question_prob": 1.0,
            "terminates_at": 99,
            "room_size": "dev",
        },
        ddqn: bool = False,
        dueling_dqn: bool = False,
        split_reward_training: bool = False,
        default_root_dir: str = "./training_results/",
    ):
        """Initialization.

        Args
        ----
        env_str: This has to be "room_env:RoomEnv-v2"
        num_iterations: The number of iterations to train the agent.
        replay_buffer_size: The size of the replay buffer.
        warm_start: The number of samples to fill the replay buffer with, before
            starting
        batch_size: The batch size for training This is the amount of samples sampled
            from the replay buffer.
        target_update_rate: The rate to update the target network.
        epsilon_decay_until: The iteration index until which to decay epsilon.
        max_epsilon: The maximum epsilon.
        min_epsilon: The minimum epsilon.
        gamma: The discount factor.
        capacity: The capacity of each human-like memory systems.
        pretrain_semantic: Whether or not to pretrain the semantic memory system.
        nn_params: The parameters for the DQN (function approximator).
        run_test: Whether or not to run test.
        num_samples_for_results: The number of samples to validate / test the agent.
        plotting_interval: The interval to plot the results.
        train_seed: The random seed for train.
        test_seed: The random seed for test.
        device: The device to run the agent on. This is either "cpu" or "cuda".
        qa_policy: question answering policy Choose one of "episodic_semantic",
            "random", or "neural". qa_policy shouldn't be trained with RL. There is no
            sequence of states / actions to learn from.
        explore_policy: The room exploration policy. Choose one of "random",
            "avoid_walls", "rl", or "neural"
        env_config: The configuration of the environment.
            question_prob: The probability of a question being asked at every
                observation.
            terminates_at: The maximum number of steps to take in an episode.
            seed: seed for env
            room_size: The room configuration to use. Choose one of "dev", "xxs", "xs",
                "s", "m", or "l".
        ddqn: wehther to use double dqn
        dueling_dqn: whether to use dueling dqn
        split_reward_training: whether to split the reward in memory management
        default_root_dir: default root directory to store the results.

        """
        all_params = deepcopy(locals())
        del all_params["self"]
        del all_params["__class__"]
        self.all_params = deepcopy(all_params)
        del all_params["split_reward_training"]
        self.split_reward_training = split_reward_training

        all_params["nn_params"]["n_actions"] = 3
        all_params["mm_policy"] = "rl"
        super().__init__(**all_params)
        write_yaml(self.all_params, os.path.join(self.default_root_dir, "train.yaml"))

        self.action2str = {0: "episodic", 1: "semantic", 2: "forget"}
        # action: 1. move to episodic, 2. move to semantic, 3. forget
        self.action_space = gym.spaces.Discrete(len(self.action2str))

    def fill_replay_buffer(self) -> None:
        """Make the replay buffer full in the beginning with the uniformly-sampled
        actions. The filling continues until it reaches the warm start size.

        """
        while len(self.replay_buffer) < self.warm_start:
            self.init_memory_systems()
            observations, info = self.env.reset()

            observations["room"] = self.manage_agent_and_map_memory(
                observations["room"]
            )

            obs = observations["room"][0]
            encode_observation(self.memory_systems, obs)
            transitions = []
            for obs in observations["room"][1:]:
                state = self.memory_systems.return_as_a_dict_list()
                action = select_dqn_action(
                    state=state,
                    greedy=False,
                    dqn=self.dqn,
                    train_val_test=self.train_val_test,
                    q_values=self.q_values,
                    epsilon=self.epsilon,
                    action_space=self.action_space,
                    save_q_value=False,
                )
                manage_memory(
                    self.memory_systems, self.action2str[action], split_possessive=False
                )
                encode_observation(self.memory_systems, obs)
                next_state = self.memory_systems.return_as_a_dict_list()
                transitions.append([state, action, None, next_state, False])

            while True:
                state = self.memory_systems.return_as_a_dict_list()
                action = select_dqn_action(
                    state=state,
                    greedy=False,
                    dqn=self.dqn,
                    train_val_test=self.train_val_test,
                    q_values=self.q_values,
                    epsilon=self.epsilon,
                    action_space=self.action_space,
                    save_q_value=False,
                )
                manage_memory(
                    self.memory_systems, self.action2str[action], split_possessive=False
                )
                action_qa = str(
                    answer_question(
                        self.memory_systems, self.qa_policy, observations["question"]
                    )
                )
                action_explore = explore(self.memory_systems, self.explore_policy)
                action_pair = (action_qa, action_explore)
                (
                    observations,
                    reward,
                    done,
                    truncated,
                    info,
                ) = self.env.step(action_pair)
                done = done or truncated

                if done or len(self.replay_buffer) >= self.warm_start:
                    break

                observations["room"] = self.manage_agent_and_map_memory(
                    observations["room"]
                )

                obs = observations["room"][0]
                encode_observation(self.memory_systems, obs)
                next_state = self.memory_systems.return_as_a_dict_list()
                transitions.append([state, action, None, next_state, done])

                for trans in transitions[:-1]:
                    if self.split_reward_training:
                        trans[2] = reward / len(transitions)
                    else:
                        trans[2] = 0
                    self.replay_buffer.store(*trans)

                trans = transitions[-1]
                if self.split_reward_training:
                    trans[2] = reward / len(transitions)
                else:
                    trans[2] = reward
                self.replay_buffer.store(*trans)

                transitions = []
                for obs in observations["room"][1:]:
                    state = self.memory_systems.return_as_a_dict_list()
                    action = select_dqn_action(
                        state=state,
                        greedy=False,
                        dqn=self.dqn,
                        train_val_test=self.train_val_test,
                        q_values=self.q_values,
                        epsilon=self.epsilon,
                        action_space=self.action_space,
                        save_q_value=False,
                    )
                    manage_memory(
                        self.memory_systems,
                        self.action2str[action],
                        split_possessive=False,
                    )
                    encode_observation(self.memory_systems, obs)
                    next_state = self.memory_systems.return_as_a_dict_list()
                    transitions.append([state, action, None, next_state, False])

    def train(self) -> None:
        """Train the memory management agent."""
        self.fill_replay_buffer()  # fill up the buffer till warm start size
        super().train()
        self.num_validation = 0

        self.epsilons = []
        self.training_loss = []
        self.scores = {"train": [], "validation": [], "test": None}

        self.dqn.train()

        training_episode_begins = True

        score = 0
        bar = trange(1, self.num_iterations + 1)
        for self.iteration_idx in bar:
            if training_episode_begins:
                self.init_memory_systems()
                observations, info = self.env.reset()

                observations["room"] = self.manage_agent_and_map_memory(
                    observations["room"]
                )

                obs = observations["room"][0]
                encode_observation(self.memory_systems, obs)
                transitions = []
                for obs in observations["room"][1:]:
                    state = self.memory_systems.return_as_a_dict_list()
                    action = select_dqn_action(
                        state=state,
                        greedy=False,
                        dqn=self.dqn,
                        train_val_test=self.train_val_test,
                        q_values=self.q_values,
                        epsilon=self.epsilon,
                        action_space=self.action_space,
                        save_q_value=True,
                    )
                    manage_memory(
                        self.memory_systems,
                        self.action2str[action],
                        split_possessive=False,
                    )
                    encode_observation(self.memory_systems, obs)
                    next_state = self.memory_systems.return_as_a_dict_list()
                    transitions.append([state, action, None, next_state, False])

            state = self.memory_systems.return_as_a_dict_list()

            action = select_dqn_action(
                state=state,
                greedy=False,
                dqn=self.dqn,
                train_val_test=self.train_val_test,
                q_values=self.q_values,
                epsilon=self.epsilon,
                action_space=self.action_space,
                save_q_value=True,
            )

            manage_memory(
                self.memory_systems, self.action2str[action], split_possessive=False
            )
            action_qa = str(
                answer_question(
                    self.memory_systems, self.qa_policy, observations["question"]
                )
            )
            action_explore = explore(self.memory_systems, self.explore_policy)
            action_pair = (action_qa, action_explore)
            (
                observations,
                reward,
                done,
                truncated,
                info,
            ) = self.env.step(action_pair)
            score += reward
            done = done or truncated

            if not done:
                observations["room"] = self.manage_agent_and_map_memory(
                    observations["room"]
                )

                obs = observations["room"][0]
                encode_observation(self.memory_systems, obs)
                next_state = self.memory_systems.return_as_a_dict_list()
                transitions.append([state, action, None, next_state, done])

                for trans in transitions[:-1]:
                    if self.split_reward_training:
                        trans[2] = reward / len(transitions)
                    else:
                        trans[2] = 0
                    self.replay_buffer.store(*trans)

                trans = transitions[-1]
                if self.split_reward_training:
                    trans[2] = reward / len(transitions)

                else:
                    trans[2] = reward
                self.replay_buffer.store(*trans)

                transitions = []
                for obs in observations["room"][1:]:
                    state = self.memory_systems.return_as_a_dict_list()
                    action = select_dqn_action(
                        state=state,
                        greedy=False,
                        dqn=self.dqn,
                        train_val_test=self.train_val_test,
                        q_values=self.q_values,
                        epsilon=self.epsilon,
                        action_space=self.action_space,
                        save_q_value=True,
                    )
                    manage_memory(
                        self.memory_systems,
                        self.action2str[action],
                        split_possessive=False,
                    )
                    encode_observation(self.memory_systems, obs)
                    next_state = self.memory_systems.return_as_a_dict_list()
                    transitions.append([state, action, None, next_state, False])

                training_episode_begins = False

            else:  # if episode ends
                self.scores["train"].append(score)
                score = 0
                with torch.no_grad():
                    self.validate()

                training_episode_begins = True

            loss = update_dqn_model(
                replay_buffer=self.replay_buffer,
                optimizer=self.optimizer,
                device=self.device,
                dqn=self.dqn,
                dqn_target=self.dqn_target,
                ddqn=self.ddqn,
                gamma=self.gamma,
            )

            self.training_loss.append(loss)

            # linearly decrease epsilon
            self.epsilon = max(
                self.min_epsilon,
                self.epsilon
                - (self.max_epsilon - self.min_epsilon) / self.epsilon_decay_until,
            )
            self.epsilons.append(self.epsilon)

            # if hard update is needed
            if self.iteration_idx % self.target_update_rate == 0:
                dqn_target_hard_update(dqn=self.dqn, dqn_target=self.dqn_target)

            # plotting & show training results
            if (
                self.iteration_idx == self.num_iterations
                or self.iteration_idx % self.plotting_interval == 0
            ):
                plot_dqn(
                    self.scores,
                    self.training_loss,
                    self.epsilons,
                    self.q_values,
                    self.iteration_idx,
                    self.action_space.n.item(),
                    self.num_iterations,
                    self.env.total_episode_rewards,
                    self.num_validation,
                    self.num_samples_for_results,
                    self.default_root_dir,
                )

        with torch.no_grad():
            self.test()

        self.env.close()

    def validate_test_middle(self) -> Tuple[List[float], Dict]:
        """A function shared by validation and test in the middle.


        Returns
        -------
        scores_temp: a list of scores
        last_memory_state: the last memory state

        """
        scores_temp = []

        for idx in range(self.num_samples_for_results):
            self.init_memory_systems()
            observations, info = self.env.reset()

            observations["room"] = self.manage_agent_and_map_memory(
                observations["room"]
            )

            if idx == self.num_samples_for_results - 1:
                save_q_value = True
            else:
                save_q_value = False

            obs = observations["room"][0]
            encode_observation(self.memory_systems, obs)
            for obs in observations["room"][1:]:
                state = self.memory_systems.return_as_a_dict_list()
                action = select_dqn_action(
                    state=state,
                    greedy=True,
                    dqn=self.dqn,
                    train_val_test=self.train_val_test,
                    q_values=self.q_values,
                    epsilon=self.epsilon,
                    action_space=self.action_space,
                    save_q_value=save_q_value,
                )
                manage_memory(
                    self.memory_systems, self.action2str[action], split_possessive=False
                )
                encode_observation(self.memory_systems, obs)

            score = 0
            while True:
                state = self.memory_systems.return_as_a_dict_list()
                action = select_dqn_action(
                    state=state,
                    greedy=True,
                    dqn=self.dqn,
                    train_val_test=self.train_val_test,
                    q_values=self.q_values,
                    epsilon=self.epsilon,
                    action_space=self.action_space,
                    save_q_value=save_q_value,
                )
                manage_memory(
                    self.memory_systems, self.action2str[action], split_possessive=False
                )
                action_qa = str(
                    answer_question(
                        self.memory_systems, self.qa_policy, observations["question"]
                    )
                )
                action_explore = explore(self.memory_systems, self.explore_policy)
                action_pair = (action_qa, action_explore)
                (
                    observations,
                    reward,
                    done,
                    truncated,
                    info,
                ) = self.env.step(action_pair)
                score += reward
                done = done or truncated

                if done:
                    break

                observations["room"] = self.manage_agent_and_map_memory(
                    observations["room"]
                )

                obs = observations["room"][0]
                encode_observation(self.memory_systems, obs)
                for obs in observations["room"][1:]:
                    state = self.memory_systems.return_as_a_dict_list()
                    action = select_dqn_action(
                        state=state,
                        greedy=True,
                        dqn=self.dqn,
                        train_val_test=self.train_val_test,
                        q_values=self.q_values,
                        epsilon=self.epsilon,
                        action_space=self.action_space,
                        save_q_value=save_q_value,
                    )
                    manage_memory(
                        self.memory_systems,
                        self.action2str[action],
                        split_possessive=False,
                    )
                    encode_observation(self.memory_systems, obs)
            scores_temp.append(score)

        return scores_temp, self.memory_systems.return_as_a_dict_list()
