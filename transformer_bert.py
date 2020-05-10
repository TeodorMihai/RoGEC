
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import argparse
import os
import time
from typing import Dict, List, Tuple

import matplotlib.pyplot as plt
import numpy as np
import tensorflow as tf
from absl import app as absl_app
from bert.tokenization.bert_tokenization import FullTokenizer
import bert
from collections import namedtuple

from transformer.dataset import construct_datasets_gec, construct_tokenizer,\
        construct_datatset_numpy, prepare_datasets, construct_tf_records
from transformer.utils import create_masks
from transformer.transformer_bert import TransformerBert
from transformer.transformer import Transformer
from transformer.transformer_scheduler import CustomSchedule
from transformer.serialization import get_ids_dataset_tf_records, upload_blob,\
                                        get_tokenizers_ckeckpoint
import beam_search


# TPU cloud params
tf.compat.v1.flags.DEFINE_string(
    "tpu", default='teodor-cotet',
    help="The Cloud TPU to use for training. This should be either the name "
    "used when creating the Cloud TPU, or a grpc://ip.address.of.tpu:8470 "
    "url.")
tf.compat.v1.flags.DEFINE_string(
    "tpu_zone", default='us-central1-f',
    help="[Optional] GCE zone where the Cloud TPU is located in. If not "
    "specified, we will attempt to automatically detect the GCE project from "
    "metadata.")
tf.compat.v1.flags.DEFINE_string(
    "gcp_project", default='rogec-271608',
    help="[Optional] Project name for the Cloud TPU-enabled project. If not "
    "specified, we will attempt to automatically detect the GCE project from "
    "metadata.")
tf.compat.v1.flags.DEFINE_bool("use_tpu", False, "Use TPUs rather than plain CPUs")
tf.compat.v1.flags.DEFINE_string('bucket', default='ro-gec', help='path from where to load bert')


# paths for datasets  1k_clean_dirty_better.txt 30k_clean_dirty_better.txt 10_mil_dirty_clean_better.txt
tf.compat.v1.flags.DEFINE_string('dataset_file', default='corpora/synthetic_wiki/30k_clean_dirty_better.txt', help='')
tf.compat.v1.flags.DEFINE_string('checkpoint', default='checkpoints/30k_transformer_64',
                help='Checpoint save locations, or restore')
tf.compat.v1.flags.DEFINE_string('bert_model_dir', default='bert/bert_ro_256/', help='path from where to load bert')
tf.compat.v1.flags.DEFINE_string('tf_records', default='corpora/tf_records/10m_bert_multi_768', help='path to tf records folder')
tf.compat.v1.flags.DEFINE_string('info', default='info.log', help='path to tf info file')

# mode of execution
"""if bert is used, the decoder is still a transofrmer with transformer specific tokenization"""
tf.compat.v1.flags.DEFINE_bool('bert', default=False, help='use bert as encoder or transformer')
tf.compat.v1.flags.DEFINE_bool('records', default=False, help='generate tf records files + tokenizers (in path tf_records)')
tf.compat.v1.flags.DEFINE_bool('train_mode', default=False, help='do training')
tf.compat.v1.flags.DEFINE_bool('decode_mode',default=False, help='do prediction, decoding')
tf.compat.v1.flags.DEFINE_bool('use_txt', default=False, help='dataset from txt file args.dataset_file')

# model params
tf.compat.v1.flags.DEFINE_integer('num_layers', default=6, help='')
tf.compat.v1.flags.DEFINE_integer('d_model', default=768,
                        help='d_model size is the out of the embeddings, it must match the bert model size, if you use one')
tf.compat.v1.flags.DEFINE_integer('seq_length', default=512, help='same as d_model')
tf.compat.v1.flags.DEFINE_integer('dff', default=2048, help='')
tf.compat.v1.flags.DEFINE_integer('num_heads', default=8, help='')
tf.compat.v1.flags.DEFINE_float('dropout', default=0.1, help='')
tf.compat.v1.flags.DEFINE_integer('dict_size', default=(2**15), help='')
tf.compat.v1.flags.DEFINE_integer('epochs', default=500, help='')
tf.compat.v1.flags.DEFINE_integer('buffer_size', default=(4 * 1024 * 1024), help='')
tf.compat.v1.flags.DEFINE_integer('batch_size', default=256, help='')
tf.compat.v1.flags.DEFINE_float('train_dev_split', default=0.97, help='')
tf.compat.v1.flags.DEFINE_integer('total_samples', default=10000000, help='')
tf.compat.v1.flags.DEFINE_bool('show_batch_stats', default=True, help='do prediction, decoding')

# deconding 100k_wiki_clean.arpa 30m_wiki_clean.arpa
tf.compat.v1.flags.DEFINE_integer('beam', default=4, help='beam width')
tf.compat.v1.flags.DEFINE_integer('max_seq_decoding', default=768, help='max length of the decoding sequence')
tf.compat.v1.flags.DEFINE_string('lm_path', default='/media/teo/drive hdd/gec/corpora/wiki_synthetic/arpa/100k_wiki_clean.arpa', 
            help='path to the the the language model arpa file')
tf.compat.v1.flags.DEFINE_bool('normalize', default=False, help='normalize reranking by sentence length')
tf.compat.v1.flags.DEFINE_float('weight_lm', default=1., help='weight of the LM in decoding (should be in [0, 2])')

# for prediction purposes only
tf.compat.v1.flags.DEFINE_string('in_file_decode', default='corpora/synthetic_wiki/50_wiki_dirty.txt', help='')
tf.compat.v1.flags.DEFINE_string('out_file_decode', default='corpora/synthetic_wiki/50_wiki_predicted.txt', help='')

# dummy values
tf.compat.v1.flags.DEFINE_string('subwords_path', default='', help='path to subwords path')
tf.compat.v1.flags.DEFINE_string('checkpoint_path', default='', help='path to checkpoint')

args = tf.compat.v1.flags.FLAGS

if args.use_tpu:
    args.subwords_path = os.path.join('gs://', args.bucket, args.checkpoint, 'corpora')
    args.checkpoint_path = os.path.join('gs://', args.bucket, args.checkpoint)
else:
    args.subwords_path = os.path.join(args.checkpoint, 'corpora')
    args.checkpoint_path = args.checkpoint


if args.d_model == 64:
    args.seq_length = 64
    args.dff = 64
    args.num_heads = 2
    args.num_layers = 2
    args.dict_size = 1024
elif args.d_model == 256:
    args.seq_length = 256 
elif args.d_model == 768:
    args.seq_length = 512

if args.decode_mode:
    args.batch_size = args.beam
    
tokenizer_pt, tokenizer_en, tokenizer_ro, tokenizer_bert = None, None, None, None
transformer, optimizer, train_loss, train_accuracy = None, None, None, None
lm_model = None
eval_loss, eval_accuracy = None, None
strategy = None
train_step_signature = [tf.TensorSpec(shape=(None, 2, args.seq_length), dtype=tf.int64),
    tf.TensorSpec(shape=(None, args.seq_length), dtype=tf.int64)]
eval_step_signature = train_step_signature


class Beam(namedtuple("Beam", ["log_prob", "ids", "length"])):
  """A finished beam

  Args:
    probs: Log probability of them beam
    finished: List of ids of the the beam
    lengths: Length of the beam
  """
  pass

def correct_from_file(in_file: str, out_file: str):
    with open(in_file, 'r') as fin, open(out_file, 'w') as fout:
        for line in fin:
            print('original: {}'.format(line))
            predicted_sentences = correct_gec(line)

def correct_gec(sentence: str, plot=''):
    global tokenizer_ro, lm_model
    import kenlm
    # install kenlm from https://github.com/kpu/kenlm

    if lm_model is None:
        lm_model = kenlm.Model(args.lm_path)
        
    beams, attention_weights = generate_sentence_beam(sentence)
    candidates = []
    for beam in beams:
        sentence_ids = []
        print(beam.ids)
        for i in beam.ids:
            if i < tokenizer_ro.vocab_size:
                sentence_ids.append(i)
            if i == tokenizer_ro.vocab_size + 1:
                break
        predicted_sentence = tokenizer_ro.decode(sentence_ids)
        print('pred: {}'.format(predicted_sentence))
        lm_prob = lm_model.score(predicted_sentence, bos=True, eos=True)

        if args.normalize:
            cand_prob = beam.log_prob + args.weight_lm * lm_prob * (1.0/beam.length)
        else:
            cand_prob = beam.log_prob + args.weight_lm * lm_prob

    return predicted_sentence

def init_beam(vocab_size, end_token_id, beam_width=1):
    
    length_penalty = 0.6 if args.normalize else 0.0
    config = beam_search.BeamSearchConfig(
        beam_width=beam_width,
        vocab_size=vocab_size,
        eos_token=end_token_id,
        length_penalty_weight=length_penalty,
        choose_successors_fn=beam_search.choose_top_k)

    beam_state = beam_search.BeamSearchState(
        log_probs=tf.nn.log_softmax(tf.ones(config.beam_width)),
        lengths=tf.constant(
            1, shape=[config.beam_width], dtype=tf.int32),
        finished=tf.zeros(
            [config.beam_width], dtype=tf.bool))
    return config, beam_state

def generate_sentence_beam(inp_sentence: str):
    global tokenizer_ro, tokenizer_bert, transformer, optimizer, args

    if tokenizer_ro is None or (args.bert and tokenizer_bert is None):
        tokenizer_ro, tokenizer_bert = get_tokenizers_ckeckpoint(args)

    if transformer is None:
        transformer, optimizer = get_model_gec()
        if args.bert:
            ckpt = tf.train.Checkpoint(decoder=transformer.decoder, 
                final_layer=transformer.final_layer, optimizer=optimizer)
        else:
            ckpt = tf.train.Checkpoint(transformer=transformer, optimizer=optimizer)
        
        ckpt_manager = tf.train.CheckpointManager(ckpt, args.checkpoint_path, max_to_keep=5)
        if ckpt_manager.latest_checkpoint:
            # loading mechanis matches variables from the tf graph and resotres their values
            ckpt.restore(ckpt_manager.latest_checkpoint)
        else:
            tf.compat.v1.logging.error('no checkpoints for transformers, aborting')
            return None

    start_token, end_token = [tokenizer_ro.vocab_size], [tokenizer_ro.vocab_size + 1]
    if args.bert:
        inp_sentence = tokenizer_bert.convert_tokens_to_ids(['[CLS]'] +
             tokenizer_bert.tokenize(inp_sentence) + ['[SEP]'])
    else:
        in_sentence = inp_sentence
        inp_sentence = start_token + tokenizer_ro.encode(inp_sentence) + end_token
        # print(tokenizer_ro.encode(in_sentence))
    start_token_id, end_token_id = tokenizer_ro.vocab_size, tokenizer_ro.vocab_size + 1

    # duplicate x beam_width == batch size
    encoder_input = tf.expand_dims(inp_sentence, 0)
    encoder_input = tf.tile(encoder_input, [args.beam, 1])

    decoder_input = [start_token_id] * args.beam
    output = tf.expand_dims(decoder_input, 1) # for batch size == beam_wisth

    # beam search init 
    config, beam_state = init_beam(vocab_size=(args.dict_size + 2),
                                                end_token_id=end_token_id, 
                                                beam_width=args.beam)
    beam_values = tf.constant(start_token_id, shape=(1, args.beam))
    beam_parents = tf.zeros((2, args.beam), dtype=tf.int32)

    for i in range(args.max_seq_decoding):
        enc_padding_mask, combined_mask, dec_padding_mask = create_masks(
            encoder_input, output)

        if args.bert:
            inp_seg = tf.zeros(shape=encoder_input.shape, dtype=tf.dtypes.int64)
            predictions, attention_weights = transformer(encoder_input, inp_seg, output,
                                                        False, enc_padding_mask, combined_mask,
                                                        dec_padding_mask)
        else:
            predictions, attention_weights = transformer(encoder_input, output,
                                                            False, enc_padding_mask,
                                                            combined_mask, dec_padding_mask)
        # !predictions.shape == (batch_size, i, vocab_size) (predicts a softmax for each existing word!)
        beam_pred = tf.squeeze(predictions[: ,-1:, :], 1)  # (batch_size, 1, vocab_size), select only the last word
        bs_output, beam_state = beam_search.beam_search_step(time_=i, logits=beam_pred,
                                                             beam_state=beam_state, config=config)

        # add new predictions to the beams decoder
        bs_output_predicted_ids = tf.expand_dims(bs_output.predicted_ids, axis=0)
        beam_values = tf.concat([beam_values, bs_output_predicted_ids], axis=0)
        res = tf.cast(beam_search.gather_tree_py(beam_values.numpy(), beam_parents.numpy()), dtype=tf.int32)
        output = tf.transpose(res)

        bs_output_beam_parent_ids = tf.expand_dims(bs_output.beam_parent_ids, axis=0)
        beam_parents = tf.concat([beam_parents, bs_output_beam_parent_ids], axis=0)

        all_finished = tf.reduce_all(beam_state.finished) # and
        if all_finished:    break

    beams = []
    for i, out in enumerate(output):
        b = Beam(log_prob=beam_state.log_probs[i].numpy(), ids=out.numpy(), length=len(out.numpy()))
        beams.append(b)

    return beams, attention_weights # return one of them

def get_model_gec():
    global args, transformer, tokenizer_ro

    vocab_size = args.dict_size + 2

    learning_rate = CustomSchedule(args.d_model)
    optimizer = tf.keras.optimizers.Adam(learning_rate, beta_1=0.9, beta_2=0.98, 
                                     epsilon=1e-9)

    if args.bert is True:
        transformer = TransformerBert(args.num_layers, args.d_model, args.num_heads, args.dff,
                            vocab_size, vocab_size,
                            model_dir=args.bert_model_dir, 
                            pe_input=vocab_size, 
                            pe_target=vocab_size,
                            rate=args.dropout, args=args)
        tf.compat.v1.logging.info('transformer bert loaded')
    else:
        transformer = Transformer(args.num_layers, args.d_model, args.num_heads, args.dff,
                            vocab_size, vocab_size, 
                            pe_input=vocab_size, 
                            pe_target=vocab_size,
                            rate=args.dropout)
    tf.compat.v1.logging.info('transformer model constructed')
    
    return transformer, optimizer

def loss_function(real, pred):
    loss_object = tf.keras.losses.SparseCategoricalCrossentropy(
        from_logits=True, reduction=tf.keras.losses.Reduction.NONE)
    
    # mask to compute loss
    mask = tf.math.logical_not(tf.math.equal(real, 0))
    loss_ = loss_object(real, pred)

    mask = tf.cast(mask, dtype=tf.float32)
    loss_ *= mask
    # loss_reduced = tf.math.divide(tf.reduce_sum(loss_), tf.reduce_sum(mask))
    loss_reduced = tf.reduce_sum(loss_)
    return loss_reduced

def acc_function(real, pred):
    pred_targets = tf.math.argmax(pred, axis=-1)
    pred_targets = tf.cast(pred_targets, tf.int64)
    
    eq = tf.equal(pred_targets, real)
    eq = tf.cast(eq, tf.int64)

    mask = tf.math.logical_not(tf.math.equal(real, 0))
    mask = tf.cast(mask, tf.int64)
    # accuracy = tf.math.divide(tf.reduce_sum(eq * mask), tf.reduce_sum(mask))
    accuracy = tf.reduce_sum(eq * mask)
    return accuracy

@tf.function(input_signature=train_step_signature)
def train_step(data, inp_segs):
    global transformer, optimizer, train_accuracy, strategy, train_loss
    # batch, seq_length
    inp, tar = data[:, 0], data[:, 1]
    tar_inp = tar[:, :-1]
    tar_real = tar[:, 1:]
    enc_padding_mask, combined_mask, dec_padding_mask = create_masks(inp, tar_inp)
    
    with tf.GradientTape() as tape:
        if args.bert is True:
            predictions, _ = transformer(inp, inp_segs, tar_inp, 
                                    True, 
                                    enc_padding_mask, 
                                    combined_mask, 
                                    dec_padding_mask)
        else:
            predictions, _ = transformer(inp, tar_inp, 
                                    True, 
                                    enc_padding_mask, 
                                    combined_mask, 
                                    dec_padding_mask)
        loss = loss_function(tar_real, predictions)
    gradients = tape.gradient(loss, transformer.trainable_variables)
    optimizer.apply_gradients(zip(gradients, transformer.trainable_variables))

    tf.compat.v1.logging.info('transformer summary: {}'.format(transformer.summary()))
    acc = acc_function(tar_real, predictions)
    if args.use_tpu == False:
        train_loss.update_state(loss)
        train_accuracy.update_state(acc)
    return loss, acc

@tf.function(input_signature=eval_step_signature)
def eval_step(data, inp_segs):
    global transformer, optimizer, eval_accuracy, eval_loss
    inp, tar = data[:, 0], data[:, 1]
    tar_inp = tar[:, :-1]
    tar_real = tar[:, 1:]
    enc_padding_mask, combined_mask, dec_padding_mask = create_masks(inp, tar_inp)

    with tf.GradientTape() as tape:
        if args.bert:
            predictions, _ = transformer(inp, inp_segs, tar_inp, 
                                    True, 
                                    enc_padding_mask, 
                                    combined_mask, 
                                    dec_padding_mask)
        else:
            predictions, _ = transformer(inp, tar_inp, 
                                    True, 
                                    enc_padding_mask, 
                                    combined_mask, 
                                    dec_padding_mask)
        loss = loss_function(tar_real, predictions)
    
    acc = acc_function(tar_real, predictions)
    if args.use_tpu == False:
        eval_loss.update_state(loss)
        eval_accuracy.update_state(acc)
    return loss, acc 

@tf.function
def distributed_train_step(dataset_inputs):
    global train_loss, train_accuracy

    data, segs = dataset_inputs
    per_example_losses, per_example_accs = strategy.experimental_run_v2(train_step, args=(data, segs))

    # per_example_accs = per_example_accs.values
    # per_example_losses = per_example_losses.values

    per_example_losses = tf.stack(per_example_losses.values, axis=0)
    per_example_accs = tf.stack(per_example_accs.values, axis=0)
    print('per_example_losses', per_example_losses)

    # print('per_example_losses', per_example_losses)
    mean_loss = strategy.reduce(tf.distribute.ReduceOp.MEAN, per_example_losses, axis=0)
    mean_acc = strategy.reduce(tf.distribute.ReduceOp.MEAN, per_example_accs, axis=0)
    return mean_loss, mean_acc

@tf.function
def distributed_eval_step(dataset_inputs):
    global eval_loss, eval_accuracy

    data, segs = dataset_inputs
    per_example_losses, per_example_accs = strategy.experimental_run_v2(eval_step, args=(data, segs))

    # per_example_accs = per_example_accs.values
    # per_example_losses = per_example_losses.values

    per_example_losses = tf.stack(per_example_losses.values, axis=0)
    per_example_accs = tf.stack(per_example_accs.values, axis=0)
    print('per_example_losses', per_example_losses)
    mean_loss = strategy.reduce(tf.distribute.ReduceOp.MEAN, per_example_losses, axis=0)
    mean_acc = strategy.reduce(tf.distribute.ReduceOp.MEAN, per_example_accs, axis=0)
    return mean_loss, mean_acc

def print_stats(args, epoch, stage, batch_idx, loss, acc, log):
    if batch_idx is not None:
        if args.show_batch_stats and (batch_idx + 1) % 100000 == 0:
            tf.compat.v1.logging.info('{} - epoch {} batch {} loss {:.4f} accuracy {:.4f}'.format(
                                stage, epoch + 1, batch_idx, loss, acc))
            log.write('{} - epoch {} batch {} loss {:.4f} accuracy {:.4f}\n'.format(
                                stage, epoch + 1, batch_idx, loss, acc))
            log.flush()
    else:
        tf.compat.v1.logging.info('Final {} - epoch {} loss {:.4f} accuracy {:.4f}'.format(
                                        stage, epoch + 1, loss, acc))
        log.write('Final {} - epoch {} loss {:.4f} accuracy {:.4f} \n'.format(
                            stage, epoch + 1, loss, acc))
        log.flush()

def train_gec():
    global args, optimizer, transformer, train_loss, train_accuracy, eval_loss, eval_accuracy, strategy
    
    with open(args.info, 'wt') as log:
        
        if args.use_txt:
            train_dataset, dev_dataset = construct_datasets_gec(args, args.subwords_path)
        else:
            train_dataset, dev_dataset, = get_ids_dataset_tf_records(args)
            train_dataset, dev_dataset = prepare_datasets(train_dataset, dev_dataset, args)
        
        for sents, seg in train_dataset.take(1):
            tf.compat.v1.logging.info('input shapes: {} {}'.format(sents.shape, seg.shape))
            tf.compat.v1.logging.info('source: {} \n target: {} \n seg: {}\n'.format(sents[0][0], sents[0][1], seg[0]))

        if args.use_tpu:
           train_dataset = strategy.experimental_distribute_dataset(train_dataset)
           dev_dataset = strategy.experimental_distribute_dataset(dev_dataset)

        train_loss = tf.keras.metrics.Mean(name='train_loss')
        train_accuracy = tf.keras.metrics.Mean(name='train_accuracy')
        eval_loss = tf.keras.metrics.Mean(name='eval_loss')
        eval_accuracy = tf.keras.metrics.Mean(name='eval_accuracy')

        transformer, optimizer = get_model_gec()
      
        # object you want to checkpoint are saved as attributes of the checkpoint obj
        if args.bert:
            ckpt = tf.train.Checkpoint(decoder=transformer.decoder, 
                                        final_layer=transformer.final_layer, optimizer=optimizer)
        else:
            ckpt = tf.train.Checkpoint(transformer=transformer, optimizer=optimizer)
       
        ckpt_manager = tf.train.CheckpointManager(ckpt, args.checkpoint_path, max_to_keep=5)
        if ckpt_manager.latest_checkpoint:
            # loading mechanis matches variables from the tf graph and resotres their values
            ckpt.restore(ckpt_manager.latest_checkpoint)
            tf.compat.v1.logging.info('latest checkpoint restored {}'.format(args.checkpoint_path))

        tf.compat.v1.logging.info('starting training...')
        eval_losses, train_losses, eval_accs, train_accs = [], [], [], []
        for epoch in range(args.epochs):
            train_loss.reset_states()
            train_accuracy.reset_states()
            eval_loss.reset_states()
            eval_accuracy.reset_states()

            # train 
            for batch_idx, data in enumerate(train_dataset):
                
                if args.use_tpu:
                    mean_loss, mean_acc = distributed_train_step(data)
                    train_losses.append(mean_loss)
                    train_accs.append(mean_acc)
                else:
                    data, inp_seg = data
                    train_step(data, inp_seg)

                # print_stats(args, epoch=epoch, stage='train', batch_idx=batch_idx,
                #              loss=train_loss.result(), acc=train_accuracy.result(), log=log)

            # print_stats(args, epoch=epoch, stage='train', batch_idx=None, 
            #                 loss=train_loss.result(), acc=train_accuracy.result(), log=log)

            if (epoch + 1) % 2 == 0:
                ckpt_save_path = ckpt_manager.save()
                log.write('Saving checkpoint for epoch {} at {} \n'.format(epoch+1,
                                                                    ckpt_save_path))
                log.flush()

            # eval
            for batch_idx, data in enumerate(dev_dataset):
                
                if args.use_tpu:
                    mean_loss, mean_acc = distributed_eval_step(data)
                    eval_losses.append(mean_loss)
                    eval_accs.append(mean_acc)
                else:
                    data, inp_seg = data
                    eval_step(data, inp_seg)

                # print_stats(args, epoch=epoch, stage='dev', batch_idx=batch_idx, 
                #            loss=eval_loss.result(), acc=eval_accuracy.result(), log=log)
            print('train losses', tf.reduce_mean(train_losses).numpy())
            print('eval losses', tf.reduce_mean(eval_losses).numpy())

            # print_stats(args, epoch=epoch, stage='dev', batch_idx=None, 
            #                 loss=eval_loss.result(), acc=eval_accuracy.result(), log=log)

def run_main():
    if args.records:
        construct_tf_records(args, args.subwords_path)

        train_tf_records = os.path.join(args.tf_records, 'train.tfrecord')
        dev_tf_records = os.path.join(args.tf_records, 'dev.tfrecord')
        tokeinizer_ro_tf_records = os.path.join(args.tf_records, 'tokenizer_ro.subwords')

        files_to_transfer = [train_tf_records, dev_tf_records, tokeinizer_ro_tf_records]

        for file_path in files_to_transfer:
            upload_blob(args.bucket, file_path, file_path)
        
        if args.bert:
            tokenizer_bert_tf_records = os.path.join(args.tf_records, 'tokenizer_bert.vocab')
            upload_blob(args.bucket, tokenizer_bert_tf_records, tokenizer_bert_tf_records)

    if args.train_mode:
        train_gec()
    if args.decode_mode:
        correct_from_file(in_file=args.in_file_decode, out_file=args.out_file_decode)
    
def main(argv):
    del argv
    global args, strategy

    if args.use_tpu == True:
        tpu_cluster_resolver = tf.distribute.cluster_resolver.TPUClusterResolver(args.tpu,
             zone=args.tpu_zone, project=args.gcp_project)
        tf.config.experimental_connect_to_cluster(tpu_cluster_resolver)
        tf.tpu.experimental.initialize_tpu_system(tpu_cluster_resolver)
        strategy = tf.distribute.experimental.TPUStrategy(tpu_cluster_resolver)
        # strategy.experimental_enable_dynamic_batch_size = False
        tf.compat.v1.logging.info('Running on TPU {}'.format(tpu_cluster_resolver.cluster_spec().as_dict()['worker']))
        tf.compat.v1.logging.info("Tpu replicas in sync: {}".format(strategy.num_replicas_in_sync))
        with strategy.scope():
            run_main()
    else:
       run_main()

if __name__ == "__main__":
    # tf.disable_v2_behavior()
    absl_app.run(main)

   
