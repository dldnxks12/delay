import torch
import torch.nn.functional as F
from replay_memory import ReplayMemory
from network import Twin_Q_net, GaussianPolicy
from utils import hard_update, soft_update
from temporary_buffer import TemporaryBuffer
class SAC:
    def __init__(self, args, state_dim, action_dim, action_bound, action_space, device):
        self.args = args

        self.state_dim = state_dim
        self.action_dim = action_dim
        self.action_bound = action_bound

        self.device = device
        self.replay_memory = ReplayMemory(state_dim, action_dim, device, args.buffer_size)
        self.eval_temporary_buffer = TemporaryBuffer(args.obs_delayed_steps)
        self.batch_size = args.batch_size

        self.gamma = args.gamma
        self.tau = args.tau

        self.actor = GaussianPolicy(args, state_dim, action_dim, action_bound, args.hidden_dims, F.relu, device).to(device)
        self.critic = Twin_Q_net(state_dim, action_dim, device, args.hidden_dims).to(device)
        self.target_critic = Twin_Q_net(state_dim, action_dim, device, args.hidden_dims).to(device)

        self.actor_optimizer = torch.optim.Adam(self.actor.parameters(), lr=args.actor_lr)
        self.critic_optimizer = torch.optim.Adam(self.critic.parameters(), lr=args.critic_lr)

        # Automated Entropy Adjustment for Maximum Entropy 1.RL-Basics
        if args.automating_temperature is True:
            self.target_entropy = -torch.prod(torch.Tensor(action_space.shape)).to(device)
            self.log_alpha = torch.zeros(1, requires_grad=True, device=device)
            self.alpha_optimizer = torch.optim.Adam([self.log_alpha], lr=args.temperature_lr)
        else:
            self.log_alpha = torch.log(torch.tensor(args.temperature, device=device, dtype=torch.float32))

        hard_update(self.critic, self.target_critic)

    def get_action(self, state, evaluation=True):
        with torch.no_grad():
            if evaluation:
                _, _, action = self.actor.sample(state)
            else:
                action, _, _ = self.actor.sample(state)
        return action.cpu().numpy()[0]

    def train_actor(self, states, train_alpha=True):
        self.actor_optimizer.zero_grad()
        actions, log_pis, _ = self.actor.sample(states)
        q_values_A, q_values_B = self.critic(states, actions)
        q_values = torch.min(q_values_A, q_values_B)

        actor_loss = (self.log_alpha.exp().detach() * log_pis - q_values).mean()
        actor_loss.backward()
        self.actor_optimizer.step()

        if train_alpha:
            self.alpha_optimizer.zero_grad()
            alpha_loss = -(self.log_alpha.exp() * (log_pis + self.target_entropy).detach()).mean()
            alpha_loss.backward()
            self.alpha_optimizer.step()
        else:
            alpha_loss = torch.tensor(0.)

    def train_critic(self, actions, rewards, dones,  states, next_states):
        self.critic_optimizer.zero_grad()
        with torch.no_grad():
            next_actions, next_log_pis, _ = self.actor.sample(next_states)
            next_q_values_A, next_q_values_B = self.target_critic(next_states, next_actions)
            next_q_values = torch.min(next_q_values_A, next_q_values_B) - self.log_alpha.exp() * next_log_pis
            target_q_values = rewards + (1 - dones) * self.gamma * next_q_values

        q_values_A, q_values_B = self.critic(states, actions)
        critic_loss = ((q_values_A - target_q_values)**2).mean() + ((q_values_B - target_q_values)**2).mean()

        critic_loss.backward()
        self.critic_optimizer.step()

    def train(self):
        actions, rewards, dones, states, next_states = self.replay_memory.sample(self.batch_size)

        self.train_critic(actions, rewards, dones, states, next_states)
        if self.args.automating_temperature is True:
            self.train_actor(states, train_alpha=True)
        else:
            self.train_actor(states, train_alpha=False)

        soft_update(self.critic, self.target_critic, self.tau)