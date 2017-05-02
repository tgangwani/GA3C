# Copyright (c) 2016, NVIDIA CORPORATION. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#  * Neither the name of NVIDIA CORPORATION nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS ``AS IS'' AND ANY
# EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT OWNER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
# PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY
# OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

from datetime import datetime
from multiprocessing import Process, Queue, Value

import numpy as np
import sys, time

from Config import Config
from Environment import Environment
from Experience import Experience


class ProcessAgent(Process):
    def __init__(self, id, prediction_q, training_q, episode_log_q):
        super(ProcessAgent, self).__init__()

        self.id = id
        self.prediction_q = prediction_q
        self.training_q = training_q
        self.episode_log_q = episode_log_q

        self.env = Environment()
        self.num_actions = self.env.get_num_actions()
        self.actions = np.arange(self.num_actions)

        self.discount_factor = Config.DISCOUNT
        # one frame at a time
        self.wait_q = Queue(maxsize=1)
        self.exit_flag = Value('i', 0)

    @staticmethod
    def _accumulate_rewards(experiences, discount_factor, value, is_running):
        if is_running:
          reward_sum = value # terminal reward
          for t in reversed(range(0, len(experiences)-1)):
              r = np.clip(experiences[t].reward, Config.REWARD_MIN, Config.REWARD_MAX) if Config.REWARD_CLIPPING else experiences[t].reward
              reward_sum = discount_factor * reward_sum + r
              experiences[t].reward = reward_sum
          return experiences[:-1]
        # if the episode has terminated, we take the full trajectory into
        # account, including the very last experience 
        else:
          reward_sum = 0
          for t in reversed(range(0, len(experiences))):
              r = np.clip(experiences[t].reward, Config.REWARD_MIN, Config.REWARD_MAX) if Config.REWARD_CLIPPING else experiences[t].reward
              reward_sum = discount_factor * reward_sum + r
              experiences[t].reward = reward_sum
          return experiences

    def convert_data(self, experiences):
        x_ = np.array([exp.state for exp in experiences])
        a_ = np.eye(self.num_actions)[np.array([exp.action for exp in experiences])].astype(np.float32)
        r_ = np.array([exp.reward for exp in experiences])
        return x_, r_, a_

    def predict(self, state, lstm_inputs):
        # put the state in the prediction q
        
        # lstm_inputs: [dict{stacklayer1}, dict{stacklayer2}, ...]
        c_state = np.array([lstm['c'] for lstm in lstm_inputs]) if len(lstm_inputs) else None
        h_state = np.array([lstm['h'] for lstm in lstm_inputs]) if len(lstm_inputs) else None
        self.prediction_q.put((self.id, state, c_state, h_state))  
        # wait for the prediction to come back
        p, v, c_state, h_state = self.wait_q.get()

        if not len(lstm_inputs):
          return p, v, []

        # convert return back to form: [dict{stack-layer1}, dict{stack-layer2}, ...]
        l = [{'c':c_state[i], 'h':h_state[i]} for i in range(c_state.shape[0])] 
        return p, v, l

    def select_action(self, prediction):
        if Config.PLAY_MODE:
            action = np.argmax(prediction)
        else:
            action = np.random.choice(self.actions, p=prediction)
        return action

    def run_episode(self):
        self.env.reset()
        is_running = True
        experiences = []

        time_count = 0
        reward_sum = 0.0

        # input states for prediction
        lstm_input_p = [{'c':np.zeros(256, dtype=np.float32),
          'h':np.zeros(256, dtype=np.float32)}]*Config.NUM_LSTMS

        # input states for training
        lstm_input_t = [{'c':np.zeros(256, dtype=np.float32),
          'h':np.zeros(256, dtype=np.float32)}]*Config.NUM_LSTMS

        while is_running:

            # very first few frames
            if self.env.current_state is None:
                _ , is_running = self.env.step(-1)  # NOOP
                assert(is_running)
                continue

            prediction, value, lstm_input_p = self.predict(self.env.current_state, lstm_input_p)
            action = self.select_action(prediction)
            reward, is_running = self.env.step(action)

            reward_sum += reward
            exp = Experience(self.env.previous_state, action, prediction, reward)
            experiences.append(exp)
            
            if not is_running or time_count == int(Config.TIME_MAX):
                updated_exps = ProcessAgent._accumulate_rewards(experiences, self.discount_factor, value, is_running)
                x_, r_, a_ = self.convert_data(updated_exps)
                yield x_, r_, a_, lstm_input_t, reward_sum, time_count 
 
                # lstm input state for next training step
                lstm_input_t = lstm_input_p
                                                                        
                # reset the tmax count
                time_count = 0
                # keep the last experience for the next batch
                experiences = [experiences[-1]]
                reward_sum = 0.0

            time_count += 1

    def run(self):
        # randomly sleep up to 1 second. helps agents boot smoothly.
        time.sleep(np.random.rand())
        np.random.seed(np.int32(time.time() % 1 * 1000 + self.id * 10))
        total_steps = 0

        while total_steps == Config.MAX_STEPS or self.exit_flag.value == 0:
            total_reward = 0
            total_length = 0
            for x_, r_, a_, lstm_, reward_sum, steps in self.run_episode():
                total_steps += steps
                total_reward += reward_sum
                total_length += len(r_) + 1  # +1 for last frame that we drop
                self.training_q.put((x_, r_, a_, lstm_))
            self.episode_log_q.put((datetime.now(), total_reward, total_length,
              total_steps))