
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

from transformer.dataset import construct_datasets_gec, construct_tokenizer_gec, prepare_tensors
from transformer.utils import create_masks, loss_function
from transformer.transformer_bert import TransformerBert
from transformer.transformer import Transformer
from transformer.transformer_scheduler import CustomSchedule


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
tf.compat.v1.flags.DEFINE_bool("test", False, "Use TPUs rather than plain CPUs")
tf.compat.v1.flags.DEFINE_string('bucket', default='ro-gec', help='path from where to load bert')


# paths for model  1k_clean_dirty_better.txt
tf.compat.v1.flags.DEFINE_string('dataset_file', default='corpora/synthetic_wiki/1k_clean_dirty_better.txt', help='')
tf.compat.v1.flags.DEFINE_string('checkpoint', default='checkpoints/transformer_test',
                help='Checpoint save locations, or restore')
# tf.compat.v1.flags.DEFINE_string('subwords', default='checkpoints/transformer_test/corpora', help='')
tf.compat.v1.flags.DEFINE_string('bert_model_dir', default='./bert/ro0/', help='path from where to load bert')

# mode of execution
"""if bert is used, the decoder is still a transofrmer with transformer specific tokenization"""
tf.compat.v1.flags.DEFINE_bool('bert', default=False, help='use bert as encoder or transformer')
tf.compat.v1.flags.DEFINE_bool('train_mode', default=False, help='do training')
tf.compat.v1.flags.DEFINE_bool('decode_mode',default=False, help='do prediction, decoding')

# model params
tf.compat.v1.flags.DEFINE_integer('num_layers', default=6, help='')
tf.compat.v1.flags.DEFINE_integer('d_model', default=256,
                        help='d_model size is the out of the embeddings, it must match the bert model size, if you use one')
tf.compat.v1.flags.DEFINE_integer('seq_length', default=256, help='same as d_model')
tf.compat.v1.flags.DEFINE_integer('dff', default=256, help='')
tf.compat.v1.flags.DEFINE_integer('num_heads', default=8, help='')
tf.compat.v1.flags.DEFINE_float('dropout', default=0.1, help='')
tf.compat.v1.flags.DEFINE_integer('dict_size', default=(2**15), help='')
tf.compat.v1.flags.DEFINE_integer('epochs', default=100, help='')
tf.compat.v1.flags.DEFINE_integer('buffer_size', default=(8*1024*1024), help='')
tf.compat.v1.flags.DEFINE_integer('batch_size', default=8, help='')
tf.compat.v1.flags.DEFINE_integer('max_length', default=256, help='')
tf.compat.v1.flags.DEFINE_float('train_dev_split', default=0.9, help='')
tf.compat.v1.flags.DEFINE_integer('total_samples', default=500, help='')
tf.compat.v1.flags.DEFINE_bool('show_batch_stats', default=True, help='do prediction, decoding')

# for prediction purposes only
tf.compat.v1.flags.DEFINE_string('in_file_decode', default='corpora/cna/dev_old/small_decode_test.txt', help='')
tf.compat.v1.flags.DEFINE_string('out_file_decode', default='corpora/cna/dev_predicted_2.txt', help='')

args = tf.compat.v1.flags.FLAGS

if args.use_tpu:
    subwords_path = 'gs://' + args.bucket + '/' + args.checkpoint + '/corpora'
    checkpoint_path = 'gs://' + args.bucket + '/' + args.checkpoint
    # args.in_file_decode = 'gs://' + args.bucket + '/' + args.in_file_decode
    # args.out_file_decode = 'gs://' + args.bucket + '/' + args.out_file_decode
else:
    subwords_path = args.checkpoint + '/corpora'
    checkpoint_path = args.checkpoint

tokenizer_pt, tokenizer_en, tokenizer_ro, tokenizer_bert = None, None, None, None
transformer, optimizer, train_loss, train_accuracy = None, None, None, None
eval_loss, eval_accuracy = None, None
strategy = None
train_step_signature = [
        tf.TensorSpec(shape=(None, args.d_model), dtype=tf.int64),
        tf.TensorSpec(shape=(None, args.d_model), dtype=tf.int64),
        tf.TensorSpec(shape=(None, None), dtype=tf.int64),
    ]
eval_step_signature = [
        tf.TensorSpec(shape=(None, args.d_model), dtype=tf.int64),
        tf.TensorSpec(shape=(None, args.d_model), dtype=tf.int64),
        tf.TensorSpec(shape=(None, None), dtype=tf.int64),
    ]


def generate_sentence_gec(inp_sentence: str):
    global tokenizer_ro, tokenizer_bert, transformer, optimizer, args, subwords_path, checkpoint_path

    if tokenizer_ro is None:
        if os.path.isfile(subwords_path + '.subwords'):
            tokenizer_ro = construct_tokenizer_gec(None, subwords_path, args)
            print('subwords restored')
        else:
            print('no subwords file, aborted')
            return

    if args.bert:
        if tokenizer_bert is None:
            tokenizer_bert = FullTokenizer(vocab_file=args.bert_model_dir + "vocab.vocab")
            tokenizer_bert.vocab_size = len(tokenizer_bert.vocab)

    if transformer is None:
        transformer, optimizer = get_model_gec()
        if args.bert:
            ckpt = tf.train.Checkpoint(decoder=transformer.decoder, final_layer=transformer.final_layer, optimizer=optimizer)
        else:
            ckpt = tf.train.Checkpoint(transformer=transformer, optimizer=optimizer)
        
        ckpt_manager = tf.train.CheckpointManager(ckpt, checkpoint_path, max_to_keep=5)
        if ckpt_manager.latest_checkpoint:
            # loading mechanis matches variables from the tf graph and resotres their values
            ckpt.restore(ckpt_manager.latest_checkpoint)
        else:
            print('No checkpoints for transformers. Aborting')
            return None
    if args.bert:
        start_token = ['[CLS]']
        end_token = ['[SEP]']
        inp_sentence = tokenizer_bert.convert_tokens_to_ids(start_token + tokenizer_bert.tokenize(inp_sentence) + end_token)
    else:
        start_token = [tokenizer_ro.vocab_size]
        end_token = [tokenizer_ro.vocab_size + 1]
        inp_sentence = start_token + tokenizer_ro.encode(inp_sentence) + end_token
    encoder_input = tf.expand_dims(inp_sentence, 0)

    # as the target is english, the first word to the transformer should be the
    # english start token.
    decoder_input = [tokenizer_ro.vocab_size]
    output = tf.expand_dims(decoder_input, 0)

    for i in range(args.seq_length):
        enc_padding_mask, combined_mask, dec_padding_mask = create_masks(
            encoder_input, output)

        # predictions.shape == (batch_size, seq_len, vocab_size)
        if args.bert:
            inp_seg = tf.zeros(shape=encoder_input.shape, dtype=tf.dtypes.int64)
            predictions, attention_weights = transformer(encoder_input, inp_seg, 
                                                            output,
                                                            False,
                                                            enc_padding_mask,
                                                            combined_mask,
                                                            dec_padding_mask)
        else:
            predictions, attention_weights = transformer(encoder_input, 
                                                            output,
                                                            False,
                                                            enc_padding_mask,
                                                            combined_mask,
                                                            dec_padding_mask)

        # select the last word from the seq_len dimension
        predictions = predictions[: ,-1:, :]  # (batch_size, 1, vocab_size)

        predicted_id = tf.cast(tf.argmax(predictions, axis=-1), tf.int32)

        # return the result if the predicted_id is equal to the end token
        if predicted_id == tokenizer_ro.vocab_size + 1:
            return tf.squeeze(output, axis=0), attention_weights

        # concatentate the predicted_id to the output which is given to the decoder
        # as its input.
        output = tf.concat([output, predicted_id], axis=-1)

    return tf.squeeze(output, axis=0), attention_weights

def correct_from_file(in_file: str, out_file: str):
    with open(in_file, 'r') as fin, open(out_file, 'w') as fout:
        for line in fin:
            predicted_sentences = correct_gec(line)
            print(line)
            print(predicted_sentences)
            #fout.write(line)
            if args.use_tpu == False:
                fout.write(predicted_sentences + '\n')
                fout.flush()

def correct_gec(sentence: str, plot=''):
    global tokenizer_ro
    result, attention_weights = generate_sentence_gec(sentence)
    predicted_sentence = tokenizer_ro.decode([i for i in result 
                                                if i < tokenizer_ro.vocab_size])  
    # print('Input: {}'.format(sentence))
    # print('Predicted sentence: {}'.format(predicted_sentence))
    
    return predicted_sentence
     
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
                            rate=args.dropout)
        print('transformer bert loaded')
    else:
        transformer = Transformer(args.num_layers, args.d_model, args.num_heads, args.dff,
                            vocab_size, vocab_size, 
                            pe_input=vocab_size, 
                            pe_target=vocab_size,
                            rate=args.dropout)
    return transformer, optimizer

def train_gec():
    global args, optimizer, transformer, train_loss, train_accuracy, eval_loss, eval_accuracy, strategy, checkpoint_path

    @tf.function(input_signature=eval_step_signature)
    def eval_step(inp, inp_seg, tar):
        global transformer, optimizer, eval_loss, eval_accuracy
        tar_inp = tar[:, :-1]
        tar_real = tar[:, 1:]

        enc_padding_mask, combined_mask, dec_padding_mask = create_masks(inp, tar_inp)

        with tf.GradientTape() as tape:
            if args.bert:
                predictions, _ = transformer(inp, inp_seg, tar_inp, 
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
        eval_loss(loss)
        eval_accuracy(tar_real, predictions)

    @tf.function(input_signature=train_step_signature)
    def train_step(inp, inp_seg, tar):
        global transformer, optimizer, train_loss, train_accuracy, strategy
        tar_inp = tar[:, :-1]
        tar_real = tar[:, 1:]

        enc_padding_mask, combined_mask, dec_padding_mask = create_masks(inp, tar_inp)
        
        with tf.GradientTape() as tape:
            if args.bert is True:
                predictions, _ = transformer(inp, inp_seg, tar_inp, 
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

        train_loss(loss)
        train_accuracy(tar_real, predictions)

    @tf.function
    def distributed_train_step(dataset_inputs):
        return strategy.run(train_step, args=(dataset_inputs,))
    
    @tf.function
    def distributed_eval_step(dataset_inputs):
        return strategy.run(eval_step, args=(dataset_inputs,))

    with open('run.txt', 'wt') as log:
        
        train_dataset, dev_dataset = construct_datasets_gec(args, subwords_path)
       
        train_loss = tf.keras.metrics.Mean(name='train_loss')
        train_accuracy = tf.keras.metrics.SparseCategoricalAccuracy(name='train_accuracy')
        eval_loss = tf.keras.metrics.Mean(name='eval_loss')
        eval_accuracy = tf.keras.metrics.SparseCategoricalAccuracy(name='eval_accuracy')

        transformer, optimizer = get_model_gec()
        # object you want to checkpoint are saved as attributes of the checkpoint obj
        if args.bert:
            ckpt = tf.train.Checkpoint(decoder=transformer.decoder, final_layer=transformer.final_layer, optimizer=optimizer)
        else:
            ckpt = tf.train.Checkpoint(transformer=transformer, optimizer=optimizer)
       
        ckpt_manager = tf.train.CheckpointManager(ckpt, checkpoint_path, max_to_keep=5)
        if ckpt_manager.latest_checkpoint:
            # loading mechanis matches variables from the tf graph and resotres their values
            ckpt.restore(ckpt_manager.latest_checkpoint)
            print('Latest checkpoint restored!!')
            # print(optimizer._decayed_lr(tf.float32))

        # train
        # for batch, data in enumerate(train_dataset.take(2)):
        #     inp, tar = tf.split(data, num_or_size_splits=2, axis=1)
        #     inps = tf.split(inp, num_or_size_splits=8, axis=0)
        #     tars = tf.split(tar, num_or_size_splits=8, axis=0)
            #inp, tar = tf.squeeze(inp), tf.squeeze(tar)
            # for i in range(0, 8):
            #     print(inps[i], tars[i])
        
        for epoch in range(args.epochs):
            start = time.time()
            train_loss.reset_states()
            train_accuracy.reset_states()
            eval_loss.reset_states()
            eval_accuracy.reset_states()

            for batch, data in enumerate(train_dataset):
                inp, tar = tf.split(data, num_or_size_splits=2, axis=1)
                inp, tar = tf.squeeze(inp), tf.squeeze(tar)
                inp_seg = tf.zeros(shape=inp.shape, dtype=tf.dtypes.int64)
                if args.use_tpu:
                    distributed_train_step([inp, inp_seg, tar])
                else:
                    train_step(inp, inp_seg, tar)
                if args.show_batch_stats and batch % 5000 == 0:
                    print('train - epoch {} batch {} loss {:.4f} accuracy {:.4f}'.format(
                        epoch + 1, batch, train_loss.result(), train_accuracy.result()))
                    log.write('train - epoch {} batch {} loss {:.4f} accuracy {:.4f}\n'.format(
                        epoch + 1, batch, train_loss.result(), train_accuracy.result()))
                    log.flush()

            if (epoch + 1) % 5 == 0:
                ckpt_save_path = ckpt_manager.save()
                log.write('Saving checkpoint for epoch {} at {} \n'.format(epoch+1,
                                                                    ckpt_save_path))
                log.flush()
            
            print('Final train - epoch {} loss {:.4f} accuracy {:.4f}'.format(epoch + 1, 
                                                            train_loss.result(), 
                                                            train_accuracy.result()))
            log.write('Final train - epoch {} loss {:.4f} accuracy {:.4f} \n'.format(epoch + 1, 
                                                            train_loss.result(), 
                                                            train_accuracy.result()))
            log.flush()
            for batch, data in enumerate(dev_dataset):
                inp, tar = tf.split(data, num_or_size_splits=2, axis=1)
                inp, tar = tf.squeeze(inp), tf.squeeze(tar)
                inp_seg = tf.zeros(shape=inp.shape, dtype=tf.dtypes.int64)
                if args.use_tpu:
                   distributed_eval_step([inp, inp_seg, tar])
                else:
                    eval_step(inp, inp_seg, tar)
                if args.show_batch_stats and batch % 1000 == 0:
                    print('Dev - epoch {} batch {} loss {:.4f} accuracy {:.4f}'.format(
                        epoch + 1, batch, eval_loss.result(), eval_accuracy.result()))
                    log.write('Dev - epoch {} batch {} loss {:.4f} accuracy {:.4f}\n'.format(
                        epoch + 1, batch, eval_loss.result(), eval_accuracy.result()))
                    log.flush()
                    
            print('Final dev - epoch {} batch {} loss {:.4f} accuracy {:.4f}'.format(
                        epoch + 1, batch, eval_loss.result(), eval_accuracy.result()))
            log.write('Final dev - epoch {} batch {} loss {:.4f} accuracy {:.4f}\n'.format(
                        epoch + 1, batch, eval_loss.result(), eval_accuracy.result()))
            log.flush()

def test_bert_trans():
    if args.bert is True:
        sample_transformer = TransformerBert(num_layers=2, d_model=512, num_heads=8, dff=2048, 
            input_vocab_size=8500, target_vocab_size=8000, 
            model_dir=args.bert_model_dir, pe_input=10000, pe_target=6000)
    else:
        sample_transformer = Transformer(
            num_layers=2, d_model=512, num_heads=8, dff=2048, 
            input_vocab_size=8500, target_vocab_size=8000, 
            pe_input=10000, pe_target=6000)

    temp_input = tf.random.uniform((64, 38), dtype=tf.int64, minval=0, maxval=200)
    temp_seg = tf.ones((64, 38), dtype=tf.int64)
    temp_target = tf.random.uniform((64, 36), dtype=tf.int64, minval=0, maxval=200)
    enc_padding_mask, combined_mask, dec_padding_mask = create_masks(temp_input, temp_target)

    if args.bert is True:
        fn_out, _ = sample_transformer(temp_input, temp_seg, temp_target, training=True, 
                                    enc_padding_mask=enc_padding_mask, 
                                    look_ahead_mask=combined_mask,
                                    dec_padding_mask=dec_padding_mask)
    else:
        fn_out, _ = sample_transformer(temp_input, temp_target, training=False, 
                                    enc_padding_mask=None, 
                                    look_ahead_mask=None,
                                    dec_padding_mask=None)

    print(fn_out.shape)  # (batch_size, tar_seq_len, target_vocab_size)

def test_transformer_dataset():
    global args 
    if args.bert is True:
        sample_transformer = TransformerBert(num_layers=2, d_model=512, num_heads=8, dff=2048, 
            input_vocab_size=8500, target_vocab_size=8000, 
            model_dir=args.bert_model_dir, pe_input=10000, pe_target=6000)
    else:
        sample_transformer = Transformer(
            num_layers=2, d_model=512, num_heads=8, dff=2048, 
            input_vocab_size=8500, target_vocab_size=8000, 
            pe_input=10000, pe_target=6000)

    inps = tf.random.uniform((1024, 38), dtype=tf.int64, minval=0, maxval=8500)
    tars = tf.random.uniform(inps.shape, dtype=tf.int64, minval=0, maxval=8000)

    dataset = tf.data.Dataset.from_tensor_slices((inps, tars))
    dataset = dataset.batch(args.batch_size, drop_remainder=True)
    dataset = dataset.map(prepare_tensors)

def run_main():
    if args.train_mode:
        # test_bert_trans()
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
        print('Running on TPU ', tpu_cluster_resolver.cluster_spec().as_dict()['worker'])
        with strategy.scope():
            if args.test:
                test_bert_trans()
            else:
                run_main()
    else:
        if args.test:
            test_bert_trans()
        else:
            run_main()

if __name__ == "__main__":
    # tf.disable_v2_behavior()
    absl_app.run(main)

   
