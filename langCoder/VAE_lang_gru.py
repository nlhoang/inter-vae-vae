import torch
import torch.nn as nn
from torch.nn import functional as F
import numpy as np
from base_model import gumbel_softmax, straight_through_discretize


def init_weights(layer):
    if type(layer) == nn.Linear:
        nn.init.kaiming_normal_(layer.weight, nonlinearity='relu')
        if layer.bias is not None:
            nn.init.zeros_(layer.bias)
    elif type(layer) == nn.LSTMCell:
        for name, param in layer.named_parameters():
            if 'weight' in name:
                nn.init.kaiming_normal_(param.data, nonlinearity='tanh')
            elif 'bias' in name:
                nn.init.zeros_(param.data)
                start, end = layer.bias_ih.size(0) // 4, layer.bias_ih.size(0) // 2
                param.data[start:end].fill_(1.)
    elif type(layer) == nn.GRUCell:
        for name, param in layer.named_parameters():
            if 'weight' in name:
                nn.init.kaiming_normal_(param.data, nonlinearity='tanh')
            elif 'bias' in name:
                nn.init.zeros_(param.data)


class VAE(nn.Module):
    def __init__(self, input_size, word_length, dictionary_size, device, temperature=1.0):
        super(VAE, self).__init__()
        self.input_size = input_size
        self.word_length = word_length
        self.dictionary_size = dictionary_size
        self.device = device
        self.temperature = temperature

        self.encoder_gru = nn.GRUCell(self.input_size, self.input_size)
        self.hidden_to_token = nn.Linear(self.input_size, self.dictionary_size)
        self.token_to_hidden = nn.Linear(self.dictionary_size, self.input_size)
        self.decoder_gru = nn.GRUCell(self.input_size, self.input_size)
        self.output_mean = nn.Linear(self.input_size, self.input_size)
        self.output_logvar = nn.Linear(self.input_size, self.input_size)
        self.apply(init_weights)

    def Encoder(self, x, sampling=True):
        logits, messages, one_hot_tokens = [], [], []
        batch_size = x.shape[0]

        hx = x
        gru_input = torch.zeros(batch_size, self.input_size, device=self.device)
        for num in range(self.word_length):
            hx = self.encoder_gru(gru_input, hx)
            pre_logits = self.hidden_to_token(hx)

            if sampling and self.training:
                z_sampled_soft = gumbel_softmax(pre_logits, self.temperature)
            else:
                z_sampled_soft = torch.softmax(pre_logits, dim=-1)

            logits.append(z_sampled_soft)
            z_sampled_onehot, word = straight_through_discretize(z_sampled_soft)
            one_hot_tokens.append(z_sampled_onehot)
            messages.append(word)
            gru_input = self.token_to_hidden(z_sampled_onehot)

        logits = torch.stack(logits).permute(1, 0, 2)
        one_hot_tokens = torch.stack(one_hot_tokens).permute(1, 0, 2)
        messages = torch.stack(messages).t()
        return one_hot_tokens, logits, messages

    def Decoder(self, z):
        batch_size = z.shape[0]
        z_embeddings = self.token_to_hidden(z.contiguous().view(-1, z.shape[-1])).view(batch_size, self.word_length, -1)
        hx = torch.zeros(batch_size, self.input_size, device=self.device)

        for n in range(self.word_length):
            inputs = z_embeddings[:, n]
            hx = self.decoder_gru(inputs, hx)

        output_mean = self.output_mean(hx)
        output_logvar = self.output_logvar(hx)
        output = self.reparameterize(output_mean, output_logvar)
        return output, output_mean, output_logvar

    def forward(self, input):
        one_hot_tokens, logits, messages = self.Encoder(input)
        recons, _, _ = self.Decoder(one_hot_tokens)
        return recons, one_hot_tokens, logits, messages

    def reparameterize(self, mu, logvar):
        if self.training:
            std = logvar.mul(0.5).exp_()
            eps = torch.empty_like(std).normal_()
            return eps.mul(std).add_(mu)
        else:
            return mu

    def elbo(self, inputs, recon, logits, beta=1):
        recon_loss = self.compute_recontruct_loss(inputs, recon)
        kld_loss = self.compute_KLD_loss(logits)
        loss = recon_loss + beta * kld_loss
        return loss, recon_loss, kld_loss

    def compute_recontruct_loss(self, inputs, recon, loss='mse'):
        if loss == 'mse':
            recon_loss = F.mse_loss(recon, inputs, reduction='sum') / inputs.size(0)
        else:
            recon_loss = F.binary_cross_entropy_with_logits(recon, inputs, reduction='sum') / inputs.size(0)
        return recon_loss

    def compute_KLD_loss(self, logits):
        logits_dist = torch.distributions.OneHotCategorical(logits=logits)
        prior = torch.log(torch.tensor([1 / self.dictionary_size] * self.dictionary_size, dtype=torch.float).repeat(1, self.word_length, 1)).to(self.device)
        prior_batch = prior.expand(logits.shape)
        prior_dist = torch.distributions.OneHotCategorical(logits=prior_batch)
        kl = torch.distributions.kl_divergence(logits_dist, prior_dist)
        return kl.sum(1).sum(0)


def train(model, dataloader, learning_rate, device, epochs=100, saved='VAE.pth', beta=1.0):
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)
    D = len(dataloader.dataset)
    for epoch in range(epochs):
        model.train()
        train_loss, train_reco, train_KLD = 0, 0, 0

        for batch_idx, data in enumerate(dataloader):
            data = data.float().to(device)
            optimizer.zero_grad()
            recon, one_hot_token, logits, message, _ = model(data)
            loss, recon_loss, kld_loss = model.elbo(inputs=data, recon=recon, logits=one_hot_token, beta=beta)
            loss.backward()
            optimizer.step()
            train_loss += loss.item()
            train_reco += recon_loss.item()
            train_KLD += kld_loss.item()

        print(f'====> Epoch: {epoch}, Avg Loss: {train_loss / D:.4f}, Recon: {train_reco / D:.4f}, KLD: {train_KLD / D:.4f}')

    torch.save(model.state_dict(), saved)


def get_messages(model, dataloader, device):
    model.eval()
    latent, message = [], []
    # attn_weights_list = []
    with torch.no_grad():
        for batch_idx, data in enumerate(dataloader):
            data = data.float().to(device)
            recon, _, logits, mes = model(data)
            latent.append(recon.cpu().numpy())
            message.append(mes.cpu().numpy())

            # batch_size = data.size(0)  # 256 in this case
            # for i in range(batch_size):  # Iterate over each sample in the batch
            #     attn_weights_sample = [attn_layer[i].cpu().numpy() for attn_layer in attn_weights]
            #     attn_weights_list.append(attn_weights_sample)

    latent = np.concatenate(latent, axis=0)
    message = np.concatenate(message, axis=0)
    return latent, message
