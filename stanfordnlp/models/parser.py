"""
Entry point for training and evaluating a dependency parser.

This implementation combines a deep biaffine graph-based parser with linearization and distance features.
For details please refer to paper: https://nlp.stanford.edu/pubs/qi2018universal.pdf.
"""

"""
Training and evaluation for the parser.
"""

import sys
import os
import shutil
import time
from datetime import datetime
import argparse
import numpy as np
import random
import torch
from torch import nn, optim

from stanfordnlp.models.lm.trainer import Trainer as LMTrainer
from stanfordnlp.models.depparse.data import DataLoader
from stanfordnlp.models.depparse.trainer import Trainer
from stanfordnlp.models.depparse import scorer
from stanfordnlp.models.common import utils
from stanfordnlp.models.common.pretrain import Pretrain


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data_dir', type=str, default='data/depparse', help='Root dir for saving models.')
    parser.add_argument('--wordvec_dir', type=str, default='extern_data/word2vec', help='Directory of word vectors')
    parser.add_argument('--train_file', type=str, default=None, help='Input file for data loader.')
    parser.add_argument('--eval_file', type=str, default=None, help='Input file for data loader.')
    parser.add_argument('--output_file', type=str, default=None, help='Output CoNLL-U file.')
    parser.add_argument('--gold_file', type=str, default=None, help='Output CoNLL-U file.')

    # additional arguments
    parser.add_argument('--vocab_cutoff', type=int, default=7, help='Word frequency threshold for vocab construction')
    parser.add_argument('--lemma_emb_dim', type=int, default=75)
    parser.add_argument('--wdecay', type=float, default=1e-6, help='weight decay applied to all weights')
    parser.add_argument('--lstm_type', type=str, default='bihlstm', choices=['hlstm', 'wdlstm', 'bihlstm'], help="LSTM type")
    parser.add_argument('--pretrain_lm', type=str, default=None, help='dir of the pretrained lm (optional)')
    parser.add_argument('--unfreeze_points', type=int, nargs='*', default=[])
    parser.add_argument('--output_hidden_dim', type=int, default=400, help='number of hidden units of the top lstm layer (only valid when lstm_type == wdlstm)')
    parser.add_argument('--deprel_loss', type=utils.bool_flag, nargs='?', const=True, default=True, help='optimize relation label loss')
    parser.add_argument('--lr_shrink', type=float, default=(1 / 2.6), help='lr shrink ratio when finetuning lower layers')

    parser.add_argument('--mode', default='train', choices=['train', 'predict'])
    parser.add_argument('--lang', type=str, help='Language')
    parser.add_argument('--shorthand', type=str, help="Treebank shorthand")
    parser.add_argument('--scorer', choices=['biaffine', 'mlp'], default='biaffine')

    parser.add_argument('--hidden_dim', type=int, default=400)
    parser.add_argument('--char_hidden_dim', type=int, default=400)
    parser.add_argument('--deep_biaff_hidden_dim', type=int, default=400)
    parser.add_argument('--word_emb_dim', type=int, default=75)
    parser.add_argument('--char_emb_dim', type=int, default=100)
    parser.add_argument('--tag_emb_dim', type=int, default=50)
    parser.add_argument('--transformed_dim', type=int, default=125)
    parser.add_argument('--num_layers', type=int, default=3)
    parser.add_argument('--char_num_layers', type=int, default=1)
    parser.add_argument('--word_dropout', type=float, default=0.33)
    parser.add_argument('--dropout', type=float, default=0.5)
    parser.add_argument('--rec_dropout', type=float, default=0, help="Recurrent dropout")
    parser.add_argument('--char_rec_dropout', type=float, default=0, help="Recurrent dropout")
    parser.add_argument('--no_char', dest='char', action='store_false', help="Turn off character model.")
    parser.add_argument('--no_pretrain', dest='pretrain', action='store_false', help="Turn off pretrained embeddings.")
    parser.add_argument('--no_linearization', dest='linearization', action='store_false', help="Turn off linearization term.")
    parser.add_argument('--no_distance', dest='distance', action='store_false', help="Turn off distance term.")

    parser.add_argument('--sample_train', type=float, default=1.0, help='Subsample training data.')
    parser.add_argument('--optim', type=str, default='adam', help='sgd, adagrad, adam or adamax.')
    parser.add_argument('--lr', type=float, default=3e-3, help='Learning rate')
    parser.add_argument('--beta1', type=float, default=0.9)
    parser.add_argument('--beta2', type=float, default=0.999)

    parser.add_argument('--max_steps', type=int, default=50000)
    parser.add_argument('--eval_interval', type=int, default=100)
    parser.add_argument('--max_steps_before_stop', type=int, default=6000)
    parser.add_argument('--batch_size', type=int, default=5000)
    parser.add_argument('--max_grad_norm', type=float, default=1.0, help='Gradient clipping.')
    parser.add_argument('--log_step', type=int, default=20, help='Print log every k steps.')
    parser.add_argument('--save_dir', type=str, default='saved_models/depparse', help='Root dir for saving models.')
    parser.add_argument('--save_name', type=str, default=None, help="File name to save the model")

    parser.add_argument('--seed', type=int, default=1234)
    parser.add_argument('--cuda', type=bool, default=torch.cuda.is_available())
    parser.add_argument('--cpu', action='store_true', help='Ignore CUDA.')
    args = parser.parse_args()
    return args


def main():
    args = parse_args()

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    random.seed(args.seed)
    if args.cpu:
        args.cuda = False
    elif args.cuda:
        torch.cuda.manual_seed(args.seed)

    print('Configuration:')
    print('\n'.join('\t{:35} {}'.format(k + ':', str(v)) for k, v in sorted(dict(vars(args)).items())))
    print()

    args = vars(args)
    print("Running parser in {} mode".format(args['mode']))

    if args['mode'] == 'train':
        train(args)
    else:
        evaluate(args)


def train(args):
    utils.ensure_dir(args['save_dir'])
    model_file = args['save_dir'] + '/' + args['save_name'] if args['save_name'] is not None \
        else '{}/{}_parser.pt'.format(args['save_dir'], args['shorthand'])

    # load pretrained language model (optional)
    if args['pretrain_lm'] is not None:
        lm_file = os.path.join(args['pretrain_lm'], 'zh_gsd_lm.pt')
        pretrain_file = os.path.join(args['pretrain_lm'], 'zh_gsd.pretrain.pt')
        pretrain = Pretrain(pretrain_file)
        use_cuda = args['cuda'] and not args['cpu']
        lm_trainer = LMTrainer(pretrain=pretrain, model_file=lm_file, use_cuda=use_cuda)
        lm_model = lm_trainer.model
        loaded_args, vocab = lm_trainer.args, lm_trainer.vocab
        train_batch = DataLoader(args['train_file'], args['batch_size'], args, pretrain,
                                 vocab=None, evaluation=False, cutoff=args['vocab_cutoff'])
        vocab['deprel'] = train_batch.vocab['deprel']
        train_batch = DataLoader(args['train_file'], args['batch_size'], args, pretrain,
                                 vocab=vocab, evaluation=False, cutoff=args['vocab_cutoff'])
    else:
        # load pretrained vectors
        vec_file = utils.get_wordvec_file(args['wordvec_dir'], args['shorthand'])
        pretrain_file = '{}/{}.pretrain.pt'.format(args['save_dir'], args['shorthand'])
        pretrain = Pretrain(pretrain_file, vec_file)
        train_batch = DataLoader(args['train_file'], args['batch_size'], args, pretrain,
                                 vocab=None, evaluation=False, cutoff=args['vocab_cutoff'])

    # load data
    print("Loading data with batch size {}...".format(args['batch_size']))
    vocab = train_batch.vocab
    train_dev_batch = DataLoader(args['train_file'], args['batch_size'], args, pretrain, vocab=vocab, evaluation=True)
    dev_batch = DataLoader(args['eval_file'], args['batch_size'], args, pretrain, vocab=vocab, evaluation=True)

    # pred and gold path
    system_pred_file = args['output_file']
    gold_file = args['gold_file']

    # skip training if the language does not have training or dev data
    if len(train_batch) == 0 or len(dev_batch) == 0:
        print("Skip training because no data available...")
        sys.exit(0)

    print("Training parser...")
    trainer = Trainer(args=args, vocab=vocab, pretrain=pretrain, use_cuda=args['cuda'], weight_decay=args['wdecay'])
    if args['pretrain_lm'] is not None:
        trainer.init_from_lm(lm_model, freeze=True)

    print()
    print('Parameters:')
    n_param = 0
    for p_name, p in trainer.model.named_parameters():
        if p.requires_grad == True:
            n_param += np.prod(list(p.size()))
            print('\t{:10}    {}'.format(p_name, p.size()))
    print('\tTotal paramamters: {}'.format(n_param))

    global_step = 0
    max_steps = args['max_steps']
    dev_score_history = []
    best_dev_preds = []
    current_lr = args['lr']
    global_start_time = time.time()
    format_str = '{}: step {}/{}, loss = {:.6f} ({:.3f} sec/batch), lr: {:.6f}'
    unfreeze_p = 0

    using_amsgrad = False
    last_best_step = 0
    # start training
    log_loss = 0
    train_loss = 0
    while True:
        do_break = False
        for i, batch in enumerate(train_batch):
            while unfreeze_p < len(args['unfreeze_points']) and global_step == args['unfreeze_points'][unfreeze_p]:
                trainer.unfreeze(args['num_layers'] - 1 - unfreeze_p, args['lr'] * args['lr_shrink']**(unfreeze_p + 1))
                unfreeze_p += 1

            start_time = time.time()
            global_step += 1
            loss = trainer.update(batch, eval=False)  # update step
            log_loss += loss
            train_loss += loss
            if global_step % args['log_step'] == 0:
                duration = time.time() - start_time
                print(format_str.format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), global_step,
                                        max_steps, log_loss / args['log_step'], duration, current_lr))
                log_loss = 0

            if global_step % args['eval_interval'] == 0:
                # eval on train
                train_preds = []
                for batch in train_dev_batch:
                    preds = trainer.predict(batch)
                    train_preds += preds

                train_dev_batch.conll.set(['head', 'deprel'], [y for x in train_preds for y in x])
                train_dev_batch.conll.write_conll(system_pred_file)
                _, _, train_score = scorer.score(system_pred_file, args['train_file'])

                # eval on dev
                print("Evaluating on dev set...")
                dev_preds = []
                for batch in dev_batch:
                    preds = trainer.predict(batch)
                    dev_preds += preds

                dev_batch.conll.set(['head', 'deprel'], [y for x in dev_preds for y in x])
                dev_batch.conll.write_conll(system_pred_file)
                _, _, dev_score = scorer.score(system_pred_file, gold_file)

                train_loss = train_loss / args['eval_interval']  # avg loss per batch
                print("step {}: train_loss = {:.6f}, train_score = {:.4f}, dev_score = {:.4f}".format(global_step, train_loss, train_score, dev_score))
                train_loss = 0

                # save best model
                if len(dev_score_history) == 0 or dev_score > max(dev_score_history):
                    last_best_step = global_step
                    trainer.save(model_file)
                    print("new best model saved.")
                    best_dev_preds = dev_preds

                dev_score_history += [dev_score]
                print("")

            if global_step - last_best_step >= args['max_steps_before_stop']:
                if not using_amsgrad:
                    print("Switching to AMSGrad")
                    last_best_step = global_step
                    using_amsgrad = True
                    trainer.optimizer = optim.Adam(trainer.model.parameters(), amsgrad=True, lr=args['lr'], betas=(.9, args['beta2']), eps=1e-6)
                else:
                    do_break = True
                    break

            if global_step >= args['max_steps']:
                do_break = True
                break

        if do_break:
            break

        train_batch.reshuffle()

    print("Training ended with {} steps.".format(global_step))

    best_f, best_eval = max(dev_score_history) * 100, np.argmax(dev_score_history) + 1
    print("Best dev F1 = {:.2f}, at iteration = {}".format(best_f, best_eval * args['eval_interval']))


def evaluate(args):
    # file paths
    system_pred_file = args['output_file']
    gold_file = args['gold_file']
    model_file = args['save_dir'] + '/' + args['save_name'] if args['save_name'] is not None \
        else '{}/{}_parser.pt'.format(args['save_dir'], args['shorthand'])
    pretrain_file = '{}/{}.pretrain.pt'.format(args['save_dir'], args['shorthand'])

    # load pretrain
    pretrain = Pretrain(pretrain_file)
    vec_file = utils.get_wordvec_file(args['wordvec_dir'], args['shorthand'])
    pretrain = Pretrain(pretrain_file, vec_file)

    # load model
    use_cuda = args['cuda'] and not args['cpu']
    trainer = Trainer(pretrain=pretrain, model_file=model_file, use_cuda=use_cuda)
    loaded_args, vocab = trainer.args, trainer.vocab

    # load config
    for k in args:
        if k.endswith('_dir') or k.endswith('_file') or k in ['shorthand'] or k == 'mode':
            loaded_args[k] = args[k]

    # load data
    print("Loading data with batch size {}...".format(args['batch_size']))
    batch = DataLoader(args['eval_file'], args['batch_size'], loaded_args, pretrain, vocab=vocab, evaluation=True)

    if len(batch) > 0:
        print("Start evaluation...")
        preds = []
        for i, b in enumerate(batch):
            preds += trainer.predict(b)
    else:
        # skip eval if dev data does not exist
        preds = []

    # write to file and score
    batch.conll.set(['head', 'deprel'], [y for x in preds for y in x])
    batch.conll.write_conll(system_pred_file)

    if gold_file is not None:
        _, _, score = scorer.score(system_pred_file, gold_file)

        print("Parser score:")
        print("{} {:.2f}".format(args['shorthand'], score * 100))

if __name__ == '__main__':
    main()
