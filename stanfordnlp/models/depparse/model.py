import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils.rnn import pad_packed_sequence, pack_padded_sequence, pack_sequence, PackedSequence

from stanfordnlp.models.common.biaffine import DeepBiaffineScorer, MLPScorer
from stanfordnlp.models.common.hlstm import HighwayLSTM
from stanfordnlp.models.common.dropout import WordDropout
from stanfordnlp.models.common.vocab import CompositeVocab
from stanfordnlp.models.common.char_model import CharacterModel
from stanfordnlp.models.common.layers import WeightDropLSTM
from stanfordnlp.models.common.rnn_utils import reverse_padded_sequence
from stanfordnlp.models.common.layers import MultiLayerLSTM


class Parser(nn.Module):

    def __init__(self, args, vocab, emb_matrix=None, share_hid=False):
        super().__init__()

        self.vocab = vocab
        self.args = args
        self.share_hid = share_hid
        self.unsaved_modules = []

        def add_unsaved_module(name, module):
            self.unsaved_modules += [name]
            setattr(self, name, module)

        # input layers
        input_size = 0
        if self.args['word_emb_dim'] > 0:
            # frequent word embeddings
            self.word_emb = nn.Embedding(len(vocab['word']), self.args['word_emb_dim'], padding_idx=0)
            input_size += self.args['word_emb_dim']

        if self.args['lemma_emb_dim'] > 0:
            self.lemma_emb = nn.Embedding(len(vocab['lemma']), self.args['lemma_emb_dim'], padding_idx=0)
            input_size += self.args['lemma_emb_dim']

        if self.args['tag_emb_dim'] > 0:
            self.upos_emb = nn.Embedding(len(vocab['upos']), self.args['tag_emb_dim'], padding_idx=0)

            if not isinstance(vocab['xpos'], CompositeVocab):
                self.xpos_emb = nn.Embedding(len(vocab['xpos']), self.args['tag_emb_dim'], padding_idx=0)
            else:
                self.xpos_emb = nn.ModuleList()

                for l in vocab['xpos'].lens():
                    self.xpos_emb.append(nn.Embedding(l, self.args['tag_emb_dim'], padding_idx=0))

            self.ufeats_emb = nn.ModuleList()

            for l in vocab['feats'].lens():
                self.ufeats_emb.append(nn.Embedding(l, self.args['tag_emb_dim'], padding_idx=0))

            input_size += self.args['tag_emb_dim'] * 2

        if self.args['char'] and self.args['char_emb_dim'] > 0:
            self.charmodel = CharacterModel(args, vocab)
            self.trans_char = nn.Linear(self.args['char_hidden_dim'], self.args['transformed_dim'], bias=False)
            input_size += self.args['transformed_dim']

        if self.args['pretrain']:
            # pretrained embeddings, by default this won't be saved into model file
            add_unsaved_module('pretrained_emb', nn.Embedding.from_pretrained(torch.from_numpy(emb_matrix), freeze=True))
            self.trans_pretrained = nn.Linear(emb_matrix.shape[1], self.args['transformed_dim'], bias=False)
            input_size += self.args['transformed_dim']

        # recurrent layers
        rnn_params = {
            'input_size': input_size,
            'hidden_size': self.args['hidden_dim'],
            'num_layers': self.args['num_layers'],
            'batch_first': True,
            'bidirectional': self.args['lstm_type'] == 'bihlstm',
            'dropout': self.args['dropout'],
            ('rec_dropout' if self.args['lstm_type'] in
                ('hlstm', 'bihlstm') else 'weight_dropout'): self.args['rec_dropout'],
        }
        if args['lstm_type'] == 'bihlstm':
            self.parserlstm = HighwayLSTM(**rnn_params, highway_func=torch.tanh)
            self.parserlstm_h_init = nn.Parameter(torch.zeros(2 * self.args['num_layers'], 1, self.args['hidden_dim']))
            self.parserlstm_c_init = nn.Parameter(torch.zeros(2 * self.args['num_layers'], 1, self.args['hidden_dim']))
        elif args['lstm_type'] == 'hlstm':
            self.lstm_forward = HighwayLSTM(**rnn_params, highway_func=torch.tanh)
            self.lstm_backward = HighwayLSTM(**rnn_params, highway_func=torch.tanh)
        elif args['lstm_type'] == 'wdlstm':
            self.lstm_forward = MultiLayerLSTM(**rnn_params, output_size=self.args['output_hidden_dim'])
            self.lstm_backward = MultiLayerLSTM(**rnn_params, output_size=self.args['output_hidden_dim'])

        self.drop_replacement = nn.Parameter(torch.randn(input_size) / np.sqrt(input_size))

        # classifiers
        hdim = self.args['output_hidden_dim'] * 2 if self.args['lstm_type'] == 'wdlstm' else self.args['hidden_dim'] * 2

        assert self.args['scorer'] in ('biaffine', 'mlp')
        if self.args['scorer'] == 'biaffine':
            self.unlabeled = DeepBiaffineScorer(hdim, hdim, self.args['deep_biaff_hidden_dim'], 1, pairwise=True, dropout=args['dropout'])
            if self.args['deprel_loss']:
                self.deprel = DeepBiaffineScorer(hdim, hdim, self.args['deep_biaff_hidden_dim'], len(vocab['deprel']), pairwise=True, dropout=args['dropout'])
        elif self.args['scorer'] == 'mlp':
            self.unlabeled = MLPScorer(hdim, hdim, self.args['deep_biaff_hidden_dim'], 1, 1, dropout=args['dropout'])
            if self.args['deprel_loss']:
                self.deprel = MLPScorer(hdim, hdim, self.args['deep_biaff_hidden_dim'], len(vocab['deprel']), 1, dropout=args['dropout'])

        if args['linearization']:
            self.linearization = DeepBiaffineScorer(hdim, hdim, self.args['deep_biaff_hidden_dim'], 1, pairwise=True, dropout=args['dropout'])
        if args['distance']:
            self.distance = DeepBiaffineScorer(hdim, hdim, self.args['deep_biaff_hidden_dim'], 1, pairwise=True, dropout=args['dropout'])

        # criterion
        self.crit = nn.CrossEntropyLoss(ignore_index=-1, reduction='sum')  # ignore padding

        self.drop = nn.Dropout(args['dropout'])
        self.worddrop = WordDropout(args['word_dropout'])

    def forward(self, word, word_mask, wordchars, wordchars_mask, upos, xpos, ufeats, pretrained, lemma, head, deprel, word_orig_idx, sentlens, wordlens):
        def pack(x):
            return pack_padded_sequence(x, sentlens, batch_first=True)

        inputs = []
        if self.args['pretrain']:
            pretrained_emb = self.pretrained_emb(pretrained)
            pretrained_emb = self.trans_pretrained(pretrained_emb)
            inputs += [pretrained_emb]

        if self.args['word_emb_dim'] > 0:
            word_emb = self.word_emb(word)
            inputs += [word_emb]

        if self.args['lemma_emb_dim'] > 0:
            lemma_emb = self.lemma_emb(lemma)
            inputs += [lemma_emb]

        if self.args['tag_emb_dim'] > 0:
            pos_emb = self.upos_emb(upos)

            if isinstance(self.vocab['xpos'], CompositeVocab):
                for i in range(len(self.vocab['xpos'])):
                    pos_emb += self.xpos_emb[i](xpos[:, :, i])
            else:
                pos_emb += self.xpos_emb(xpos)

            feats_emb = 0
            for i in range(len(self.vocab['feats'])):
                feats_emb += self.ufeats_emb[i](ufeats[:, :, i])

            inputs += [pos_emb, feats_emb]

        if self.args['char'] and self.args['char_emb_dim'] > 0:
            char_reps = self.charmodel(wordchars, wordchars_mask, word_orig_idx, sentlens, wordlens)
            char_reps = self.trans_char(self.drop(char_reps.data))
            inputs += [char_reps]

        lstm_inputs = torch.cat(inputs, -1)
        lstm_inputs = self.worddrop(lstm_inputs, self.drop_replacement)
        lstm_inputs = self.drop(lstm_inputs)
        rev_lstm_inputs = reverse_padded_sequence(lstm_inputs, sentlens, batch_first=True)

        if self.args['lstm_type'] == 'bihlstm':
            lstm_inputs = pack(lstm_inputs)
            lstm_outputs, _ = self.parserlstm(lstm_inputs, sentlens, hx=(self.parserlstm_h_init.expand(
                2 * self.args['num_layers'], word.size(0), self.args['hidden_dim']).contiguous(),
                self.parserlstm_c_init.expand(2 * self.args['num_layers'], word.size(0), self.args['hidden_dim']).contiguous()))
            lstm_outputs, _ = pad_packed_sequence(lstm_outputs, batch_first=True)
        elif self.args['lstm_type'] == 'hlstm':
            rev_lstm_inputs = pack(rev_lstm_inputs)
            hid_forward, _ = self.lstm_forward(lstm_inputs, sentlens)
            hid_backward, _ = self.lstm_backward(lstm_inputs, sentlens)
            hid_forward, _ = pad_packed_sequence(hid_forward, batch_first=True)
            hid_backward, _ = pad_packed_sequence(hid_backward, batch_first=True)
            hid_backward = reverse_padded_sequence(hid_backward, sentlens, batch_first=True)
            lstm_outputs = torch.cat([hid_forward, hid_backward], -1)
        elif self.args['lstm_type'] == 'wdlstm':
            hid_forward, _ = self.lstm_forward(lstm_inputs)
            hid_backward, _ = self.lstm_backward(rev_lstm_inputs)
            hid_backward = reverse_padded_sequence(hid_backward, sentlens, batch_first=True)
            lstm_outputs = torch.cat([hid_forward, hid_backward], -1)

        unlabeled_scores = self.unlabeled(self.drop(lstm_outputs), self.drop(lstm_outputs)).squeeze(3)

        if self.args['deprel_loss']:
            deprel_scores = self.deprel(self.drop(lstm_outputs), self.drop(lstm_outputs))

        if self.args['linearization'] or self.args['distance']:
            head_offset = torch.arange(word.size(1), device=head.device).view(1, 1, -1).expand(word.size(0), -1, -1) - \
                torch.arange(word.size(1), device=head.device).view(1, -1, 1).expand(word.size(0), -1, -1)

        if self.args['linearization']:
            lin_scores = self.linearization(self.drop(lstm_outputs), self.drop(lstm_outputs)).squeeze(3)
            unlabeled_scores += F.logsigmoid(lin_scores * torch.sign(head_offset).float()).detach()

        if self.args['distance']:
            dist_scores = self.distance(self.drop(lstm_outputs), self.drop(lstm_outputs)).squeeze(3)
            dist_pred = 1 + F.softplus(dist_scores)
            dist_target = torch.abs(head_offset)
            dist_kld = -torch.log((dist_target.float() - dist_pred)**2 / 2 + 1)
            unlabeled_scores += dist_kld.detach()

        diag = torch.eye(head.size(-1) + 1, dtype=torch.uint8, device=head.device).unsqueeze(0)
        unlabeled_scores.masked_fill_(diag, -float('inf'))

        preds = []

        if self.training:
            unlabeled_scores = unlabeled_scores[:, 1:, :]  # exclude attachment for the root symbol
            unlabeled_scores = unlabeled_scores.masked_fill(word_mask.unsqueeze(1), -float('inf'))
            unlabeled_target = head.masked_fill(word_mask[:, 1:], -1)
            loss = self.crit(unlabeled_scores.contiguous().view(-1, unlabeled_scores.size(2)), unlabeled_target.view(-1))

            if self.args['deprel_loss']:
                deprel_scores = deprel_scores[:, 1:]  # exclude attachment for the root symbol
                deprel_scores = torch.gather(deprel_scores, 2, head.unsqueeze(2).unsqueeze(3).expand(-1, -1, -1, len(self.vocab['deprel']))).view(-1, len(self.vocab['deprel']))
                deprel_target = deprel.masked_fill(word_mask[:, 1:], -1)
                loss += self.crit(deprel_scores.contiguous(), deprel_target.view(-1))

            if self.args['linearization']:
                lin_scores = torch.gather(lin_scores[:, 1:], 2, head.unsqueeze(2)).view(-1)
                lin_scores = torch.cat([-lin_scores.unsqueeze(1) / 2, lin_scores.unsqueeze(1) / 2], 1)
                lin_target = torch.gather((head_offset[:, 1:] > 0).long(), 2, head.unsqueeze(2))
                loss += self.crit(lin_scores.contiguous(), lin_target.view(-1))

            if self.args['distance']:
                dist_kld = torch.gather(dist_kld[:, 1:], 2, head.unsqueeze(2))
                loss -= dist_kld.sum()

            loss /= wordchars.size(0)  # number of words
        else:
            loss = 0
            preds.append(F.log_softmax(unlabeled_scores, 2).detach().cpu().numpy())
            if self.args['deprel_loss']:
                preds.append(deprel_scores.max(3)[1].detach().cpu().numpy())
            else:
                preds.append(np.random.randn(*preds[0].shape, len(self.vocab['deprel'])).argmax(-1))

        return loss, preds
