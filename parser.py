#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import teras.logging as Log
import teras.utils
from teras.app import App, arg
from teras.training import Trainer, TrainEvent as Event

import utils


def train(
        train_file,
        test_file=None,
        embed_file=None,
        embed_size=100,
        n_epoch=20,
        batch_size=32,
        lr=0.002,
        model_params={},
        gpu=-1,
        save_to=None,
        seed=None,
        backend='chainer'):
    if backend == 'chainer':
        import chainer
        import chainer_model as models
        import teras.framework.chainer as framework_utils
        framework_utils.set_debug(App.debug)
        if seed is not None:
            import random
            import numpy
            random.seed(seed)
            numpy.random.seed(seed)
            if gpu >= 0:
                try:
                    import cupy
                    cupy.cuda.runtime.setDevice(gpu)
                    cupy.random.seed(seed)
                except Exception as e:
                    Log.e(str(e))
            Log.i("random seed: {}".format(seed))
    elif backend == 'pytorch':
        import torch
        import pytorch_model as models
        import teras.framework.pytorch as framework_utils
        if seed is not None:
            import random
            import numpy
            random.seed(seed)
            numpy.random.seed(seed)
            torch.manual_seed(seed)
            Log.i("random seed: {}".format(seed))
    else:
        raise ValueError("backend={} is not supported."
                         .format(backend))

    # Load files
    Log.i('initialize DataLoader with embed_file={} and embed_size={}'
          .format(embed_file, embed_size))
    loader = utils.DataLoader(word_embed_file=embed_file,
                              word_embed_size=embed_size,
                              pos_embed_size=embed_size)
    Log.i('load train dataset from {}'.format(train_file))
    train_dataset = loader.load(train_file, train=True)
    if test_file:
        Log.i('load test dataset from {}'.format(test_file))
        test_dataset = loader.load(test_file, train=False)
    else:
        test_dataset = None

    model_cls = models.DeepBiaffine

    Log.v('')
    Log.v("initialize ...")
    Log.v('--------------------------------')
    Log.i('# Minibatch-size: {}'.format(batch_size))
    Log.i('# epoch: {}'.format(n_epoch))
    Log.i('# gpu: {}'.format(gpu))
    Log.i('# model: {}'.format(model_cls))
    Log.i('# model params: {}'.format(model_params))
    Log.v('--------------------------------')
    Log.v('')

    # Set up a neural network model
    model = model_cls(
        embeddings=({'initialW':
                         loader.get_embeddings('word_pretrained', normalize='l2'),
                     'fixed_weight': True},
                    {'initialW': loader.get_embeddings('word'),
                     'fixed_weight': False},
                    loader.get_embeddings('pos')),
        n_labels=len(loader.label_map),
        **model_params,
    )
    if gpu >= 0:
        framework_utils.set_model_to_device(model, device_id=gpu)

    # Setup an optimizer
    if backend == 'chainer':
        optimizer = chainer.optimizers.Adam(
            alpha=lr, beta1=0.9, beta2=0.9, eps=1e-12)
        optimizer.setup(model)
        optimizer.add_hook(chainer.optimizer.GradientClipping(5.0))
        optimizer.add_hook(
            framework_utils.optimizers.ExponentialDecayAnnealing(
                initial_lr=lr, decay_rate=0.75, decay_step=5000,
                lr_key='alpha'))
    elif backend == 'pytorch':
        optimizer = torch.optim.Adam(model.parameters(),
                                     lr=lr, betas=(0.9, 0.9), eps=1e-12)
        torch.nn.utils.clip_grad_norm(model.parameters(), max_norm=5.0)

        class Annealing(object):

            def __init__(self, optimizer):
                self.step = 0
                self.optimizer = optimizer

            def __call__(self, data):
                if not data['train']:
                    return
                self.step = self.step + 1
                decay, decay_step = 0.75, 5000
                decay_rate = decay ** (self.step / decay_step)
                for param_group in self.optimizer.param_groups:
                    param_group['lr'] = lr * decay_rate

        annealing = Annealing(optimizer)
    Log.i('optimizer: Adam(alpha={}, beta1=0.9, '
          'beta2=0.9, eps=1e-12), grad_clip=5.0'.format(lr))

    # Setup a trainer
    parser = models.BiaffineParser(model)

    trainer = Trainer(optimizer, parser, loss_func=parser.compute_loss,
                      accuracy_func=parser.compute_accuracy)
    trainer.configure(framework_utils.config)
    if backend == 'pytorch':
        trainer.add_hook(Event.EPOCH_TRAIN_BEGIN, lambda data: model.train())
        trainer.add_hook(Event.EPOCH_VALIDATE_BEGIN, lambda data: model.eval())
        trainer.add_hook(Event.BATCH_BEGIN, annealing)
    if test_dataset:
        trainer.attach_callback(
            utils.Evaluator(parser,
                            pos_map=loader.get_processor('pos').vocabulary,
                            ignore_punct=True))

    if save_to is not None:
        accessid = Log.getLogger().accessid
        date = Log.getLogger().accesstime.strftime('%Y%m%d')
        trainer.attach_callback(
            framework_utils.callbacks.Saver(
                model,
                basename="{}-{}".format(date, accessid),
                directory=save_to,
                context=dict(App.context, model_cls=model_cls, loader=loader)))

    # Start training
    trainer.fit(train_dataset, None,
                batch_size=batch_size,
                epochs=n_epoch,
                validation_data=test_dataset,
                verbose=App.verbose)


def test(
        model_file,
        target_file,
        decode=False,
        gpu=-1):
    # Load context
    context = teras.utils.load_context(model_file)
    if context.backend == 'chainer':
        import chainer
        import chainer_model as models
        import teras.framework.chainer as framework_utils
        framework_utils.set_debug(App.debug)

        def _load_test_model(model, file, device_id=-1):
            chainer.serializers.load_npz(file, model)
            framework_utils.set_model_to_device(model, device_id)
            framework_utils.chainer_train_off()
    elif context.backend == 'pytorch':
        import torch
        import pytorch_model as models
        import teras.framework.pytorch as framework_utils

        def _load_test_model(model, file, device_id=-1):
            model.load_state_dict(torch.load(file))
            framework_utils.set_model_to_device(model, device_id)
            model.eval()
    else:
        raise ValueError("backend={} is not supported."
                         .format(context.backend))

    # Load files
    Log.i('load dataset from {}'.format(target_file))
    loader = context.loader
    dataset = loader.load(target_file, train=False)

    Log.v('')
    Log.v("initialize ...")
    Log.v('--------------------------------')
    Log.i('# gpu: {}'.format(gpu))
    Log.i('# model: {}'.format(context.model_cls))
    Log.i('# context: {}'.format(context))
    Log.v('--------------------------------')
    Log.v('')

    # Set up a neural network model
    model = context.model_cls(
        embeddings=({'initialW':
                         loader.get_embeddings('word_pretrained', normalize='l2'),
                     'fixed_weight': True},
                    {'initialW': loader.get_embeddings('word'),
                     'fixed_weight': False},
                    loader.get_embeddings('pos')),
        n_labels=len(loader.label_map),
        **context.model_params,
    )
    _load_test_model(model, model_file, device_id=gpu)

    parser = models.BiaffineParser(model)
    pos_map = loader.get_processor('pos').vocabulary
    label_map = loader.label_map
    evaluator = utils.Evaluator(parser, pos_map, ignore_punct=True)

    # Start testing
    UAS, LAS, count = 0.0, 0.0, 0.0
    for batch_index, batch in enumerate(
            dataset.batch(context.batch_size, shuffle=False)):
        pretrained_word_tokens, word_tokens, pos_tokens = batch[:-1]
        true_arcs, true_labels = batch[-1].T
        arcs_batch, labels_batch = parser.parse(
            pretrained_word_tokens, word_tokens, pos_tokens)
        for i, (p_arcs, p_labels, t_arcs, t_labels) in enumerate(
                zip(arcs_batch, labels_batch, true_arcs, true_labels)):
            mask = evaluator.create_ignore_mask(pos_tokens[i])
            _uas, _las, _count = evaluator.evaluate(
                p_arcs, p_labels, t_arcs, t_labels, mask)
            if decode:
                words = loader.get_sentence(word_tokens[i])
                for word, pos_id, arc, label_id in zip(
                        words[1:], pos_tokens[i][1:],
                        p_arcs[1:], p_labels[1:]):
                    print("\t".join([word, pos_map.lookup(pos_id),
                                     str(arc), label_map.lookup(label_id)]))
                print()
            UAS, LAS, count = UAS + _uas, LAS + _las, count + _count
    Log.i("[evaluation] UAS: {:.8f}, LAS: {:.8f}"
          .format(UAS / count * 100, LAS / count * 100))


if __name__ == "__main__":
    Log.AppLogger.configure(mkdir=True)

    App.add_command('train', train, {
        'backend':
            arg('--backend', type=str,
                choices=('chainer', 'pytorch'), default='chainer',
                help='Backend framework for computation'),
        'batch_size':
            arg('--batchsize', '-b', type=int, default=32,
                help='Number of examples in each mini-batch'),
        'embed_file':
            arg('--embedfile', type=str, default=None,
                help='Pretrained word embedding file'),
        'embed_size':
            arg('--embedsize', type=int, default=100,
                help='Size of embeddings'),
        'gpu':
            arg('--gpu', '-g', type=int, default=-1,
                help='GPU ID (negative value indicates CPU)'),
        'lr':
            arg('--lr', type=float, default=0.002,
                help='Learning Rate'),
        'model_params':
            arg('--model', action='store_dict', default={},
                help='Model hyperparameter'),
        'n_epoch':
            arg('--epoch', '-e', type=int, default=20,
                help='Number of sweeps over the dataset to train'),
        'seed':
            arg('--seed', type=int, default=None,
                help='Random seed'),
        'save_to':
            arg('--out', type=str, default=None,
                help='Save model to the specified directory'),
        'test_file':
            arg('--validfile', type=str, default=None,
                help='validation data file'),
        'train_file':
            arg('--trainfile', type=str, required=True,
                help='training data file'),
    })

    App.add_command('test', test, {
        'decode':
            arg('--decode', action='store_true', default=False,
                help='Print decoded results'),
        'gpu':
            arg('--gpu', '-g', type=int, default=-1,
                help='GPU ID (negative value indicates CPU)'),
        'model_file':
            arg('--modelfile', type=str, required=True,
                help='Trained model archive file'),
        'target_file':
            arg('--targetfile', type=str, required=True,
                help='Decoding target data file'),
    })

    App.run()
