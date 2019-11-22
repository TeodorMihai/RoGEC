import json
from pprint import pprint

import numpy as np
import os
import re
import spacy
import csv
from collections import Counter

import tensorflow as tf
from tensorflow import keras 
from tensorflow.keras.layers import Dense, Dropout, Embedding, LSTM, Bidirectional, GRU, Input
from tensorflow.keras.layers import TimeDistributed, concatenate, Reshape, RepeatVector
from tensorflow.keras.models import Sequential
from tensorflow.keras.preprocessing.text import Tokenizer
from tensorflow.keras.preprocessing.sequence import pad_sequences
from tensorflow.keras.callbacks import ModelCheckpoint, LambdaCallback, Callback

from keras.backend.tensorflow_backend import set_session

from gensim.models.keyedvectors import KeyedVectors

from sklearn.model_selection import StratifiedShuffleSplit
from sklearn.model_selection import StratifiedKFold
from sklearn import datasets
from sklearn.naive_bayes import MultinomialNB
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn import svm
from typing import List
from gensim.models.wrappers import FastText as FastTextWrapper

import random
import argparse
args = None




class Model:
    FAST_TEXT = "fasttext_fb/wiki.ro"
    # filename is an elasticsearch dump, use npm elasticdump
    MAX_SENT_TOKENS = 18
    MAX_CHARS_TOKENS = 30
    MAX_ALLOWED_CHAR = 600
    # odd
    WIN_CHARS = 29
    GRU_CELL_SIZE = 64
    PATIENCE = 4
    EPOCHS = 100
    BATCH_SIZE = 64
    DENSES = [128, 64]
    EMB_CHARS_SIZE = 28
    MAX_CHAR = 1000
    START_CHAR = 1000
    END_CHAR = 1001
    SRC_TEXT_CHAR_LENGTH = 300


    CORRECT_DIACS = {
        "ş": "ș",
        "Ş": "Ș",
        "ţ": "ț",
        "Ţ": "Ț",
    }


    def __init__(self): 
        config = tf.ConfigProto()
        #config.gpu_options.per_process_gpu_memory_fraction = 0.2
        config.gpu_options.allow_growth = True
        set_session(tf.Session(config=config))

    # elasticsearch dump file
    def load_data(self, filename):
        global args
        id2word, word2id = {}, {}
        count = 0
        text_in, text_out, wrong_words, correct_words = [], [], [], []
        with open(filename, "r", encoding='utf-8') as csv_file:
            csv_reader = csv.reader(csv_file, delimiter=',')
            for jj, row in enumerate(csv_reader):
                inn = self.clean_text(row[1].lower())
                out = self.clean_text(row[0].lower())
                in_tokens = inn.split()
                out_tokens = out.split()
                if len(in_tokens) != len(out_tokens):
                    continue
                if args.small_run == True and jj > 10000:
                    continue
                if jj > 300e3:
                    continue

                for token in out_tokens:
                    if token not in word2id:
                        word2id[token] = count
                        id2word[count] = token
                        count += 1
                cc = Counter(in_tokens)
                for i, token in enumerate(in_tokens):
                    # keep only if token does not repeat
                    if token != out_tokens[i] and cc[token] == 1:
                        text_in.append(in_tokens)
                        text_out.append(out_tokens)
                        wrong_words.append(token)
                        correct_words.append(out_tokens[i])
        print(len(text_in))
        
        return text_in, text_out, wrong_words, correct_words, id2word, word2id
    
    def clean_text(self, text: str):
        list_text = list(text)
        text = "".join([Model.CORRECT_DIACS[c] if c in Model.CORRECT_DIACS else c for c in list_text])
        # list_text = list(text)
        # list_text = [c for c in text if ord(c) < Model.MAX_CHAR]
        # text = "".join(list_text)
        # some cleaning correct diacritics + eliminate \
        return text.lower()
         
    def construct_lemma_dict(self, lemma_file="wordlists/lemmas_ro.txt"):
        self.word_to_lemma = {}
        self.lemma_to_words = {}

        with open(lemma_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.split()
                if len(line) != 2:
                    continue

                line[0] = self.clean_text(line[0].strip())
                line[1] = self.clean_text(line[1].strip())
               
                self.word_to_lemma[line[0]] = line[1]
                self.word_to_lemma[line[1]] = line[1]

                if line[1] not in self.lemma_to_words:
                    self.lemma_to_words[line[1]] = [line[0]]
                else:
                    self.lemma_to_words[line[1]].append(line[0])
                
                if line[1] not in self.lemma_to_words:
                    self.lemma_to_words[line[1]] = [line[1]]
                else:
                    self.lemma_to_words[line[1]].append(line[1])

        print('lemmas: {}'.format(len(self.lemma_to_words)))
        print('words: {}'.format(len(self.word_to_lemma)))

    def split_dataset(self, text_in, text_out, wrong_words, correct_words):
        n = len(text_in)
        n1 = int(n * 0.97)

        return text_in[:n1], text_out[:n1], wrong_words[:n1], correct_words[:n1],\
                text_in[n1:], text_out[:n1], wrong_words[n1:], correct_words[n1:]

    def construct_window_chars(self, sample, index):
        win_chars = []
        strr = " ".join(sample)
        side = Model.WIN_CHARS // 2

        for i in range(index - side, index + side + 1):
            if i < 0 or i >= len(strr):
                v1 = 0
            elif ord(strr[i]) > Model.MAX_ALLOWED_CHAR:
                v1 = 0
            else:
                v1 = ord(strr[i])
            win_chars.append(v1)
        return win_chars        
    
    def construct_input_chars(self, in_tokens_sent, in_ww, in_cw):
        global args

        pass

    def construct_input(self, in_tokens_sent, in_ww, in_cw, do_shuffle=False):
        global args

        if args.small_run == False:
            self.fasttext = FastTextWrapper.load_fasttext_format(Model.FAST_TEXT)
        
        inputs_sent, in_emb_ww, chars_wind = [], [], []
        ins_cw, sent, ins_ww = [], [], []
        out = []
        chars = list(in_tokens_sent)

        for i, sample in enumerate(in_tokens_sent):
            for predict_token in sample:
                pos_token = 0
                inn = np.zeros((Model.MAX_SENT_TOKENS, 300))

                # predict only for words with lemma
                if predict_token not in self.word_to_lemma:
                    continue

                for j, token in enumerate(sample):
                    if args.small_run == True:
                        inn[Model.MAX_SENT_TOKENS - j - 1][:] = np.float32([0] * 300)
                    else:
                        try:
                            inn[Model.MAX_SENT_TOKENS - j - 1][:] = np.float32(self.fasttext.wv[token])
                        except:
                            inn[Model.MAX_SENT_TOKENS - j - 1][:] = np.float32([0] * 300)

                    #if token == in_ww[i]:
                    if token == predict_token:
                        char_w = self.construct_window_chars(sample, pos_token)
                        try:
                            w_emb = np.float32(self.fasttext.wv[token])
                        except:
                            w_emb = np.float32([0] * 300)

                        if len(out) == len(in_emb_ww):
                            if predict_token == in_ww[i]:
                                out.append(1)
                            else:
                                out.append(0)

                    pos_token += len(token) + 1
                # take 5x same examples if the class is 1
                if out[-1] == 1:
                    take_samples = 7
                else:
                    take_samples = 1

                for _ in range(take_samples):
                    sent.append(sample)
                    ins_ww.append(predict_token)
                    in_emb_ww.append(w_emb)
                    inputs_sent.append(inn)
                    chars_wind.append(char_w)
                    if out[-1] == 1:
                        ins_cw.append(in_cw[i])
                    else:
                        ins_cw.append(predict_token)

                for _ in range(take_samples - 1):
                    out.append(out[-1])
                
        print(Counter(out))
        out = keras.utils.to_categorical(out, num_classes=2)
        allin = [(inputs_sent[i], in_emb_ww[i], chars_wind[i], out[i]) for i, _ in enumerate(inputs_sent)]
        if do_shuffle == True:
            random.shuffle(allin)
        inputs_sent = [inn[0] for inn in allin]
        in_emb_ww = [inn[1] for inn in allin]
        chars_wind = [inn[2] for inn in allin]
        out = [inn[3] for inn in allin]

        return [np.asarray(inputs_sent), np.asarray(in_emb_ww), np.asarray(chars_wind)], out, ins_cw, ins_ww, sent
    
    def construct_output(self, train_cw, word2id):
        out = []
        for i, x in enumerate(train_cw):
            out.append(word2id[x])
        out = keras.utils.to_categorical(out, num_classes=len(word2id))
        return out

    def run_model_rnn(self):
        global args
        self.construct_lemma_dict()
        text_in, text_out, wrong_words, correct_words, id2word, word2id = self.load_data(filename=args.input_file)
        voc_size = len(id2word)
        """load dataset"""
        train_in, train_ww, train_cw, train_ww, test_in, test_ww, test_cw , test_ww = \
            self.split_dataset(text_in, text_out, wrong_words, correct_words)

        """ train """
        if args.no_train == False:
            sentence_embeddings_layer = Input(shape=((Model.MAX_SENT_TOKENS, 300,)))
            sentence_lstm_layer = GRU(units=Model.GRU_CELL_SIZE, input_shape=(Model.MAX_SENT_TOKENS, 300,))
            bi_lstm_layer_sent = keras.layers.Bidirectional(layer=sentence_lstm_layer,\
                                        merge_mode="concat")(sentence_embeddings_layer)
            word_emb = Input(shape=(300,))

            input_character_window = keras.layers.Input(shape=(Model.WIN_CHARS,))
            character_embeddings_layer = keras.layers.Embedding(
                                            input_dim=Model.MAX_ALLOWED_CHAR + 1,\
                                            output_dim=Model.EMB_CHARS_SIZE)(input_character_window)
            chars_lstm_layer = GRU(units=Model.GRU_CELL_SIZE, input_shape=(Model.MAX_CHARS_TOKENS, 300,))
            bi_lstm_layer_chars = keras.layers.Bidirectional(layer=chars_lstm_layer,\
                                        merge_mode="concat")(character_embeddings_layer)
            conc = keras.layers.concatenate([bi_lstm_layer_sent, word_emb, bi_lstm_layer_chars], axis=-1)
                    
            conc = keras.layers.Dropout(0.2)(conc)
            conc = keras.layers.Dense(Model.DENSES[0], activation='tanh')(conc)
            conc = keras.layers.Dropout(0.1)(conc)
            d1 = keras.layers.Dense(Model.DENSES[1], activation='tanh')(conc)                                                 
            output = keras.layers.Dense(2, activation='softmax')(d1)
            train_inn, train_out, _, _, _= self.construct_input(train_in, train_ww, train_cw)
            #train_out = self.construct_output(train_cw, word2id)
            callbacks = [keras.callbacks.EarlyStopping(monitor='val_loss', patience=Model.PATIENCE)]
            inn = [sentence_embeddings_layer, word_emb, input_character_window]

            model = keras.models.Model(inputs=inn,
                                    outputs=output)
            model.compile(optimizer='adam',\
                        loss='categorical_crossentropy',\
                        metrics=['accuracy'])
            print(model.summary())

            model.fit(train_inn,
                    [train_out],
                    batch_size=Model.BATCH_SIZE, 
                    epochs=Model.EPOCHS, 
                    validation_split=0.2,
                    callbacks=callbacks)
        else:
            model = keras.models.load_model(args.load)

        """ compute stats """
        with open(args.test, "w", encoding='utf-8') as g:
            tp, tn, fp, fn = 0, 0, 0, 0
            test_inn, test_out, ins_cw, ins_ww, sent = self.construct_input(test_in, test_ww, test_cw, do_shuffle=False)
            print(len(test_inn))
            for i in range(len(test_inn[0])):
                if args.only_word == True:
                    in1 = test_inn[1][i].reshape((1, -1))
                    inn = [in1]
                else:
                    in1 = test_inn[0][i].reshape((1, Model.MAX_SENT_TOKENS, -1))
                    in2 = test_inn[1][i].reshape((1, -1))
                    in3 = test_inn[2][i].reshape((1, -1))
                    inn = [in1, in2, in3]
                p = model.predict(x=inn, batch_size=None, steps=1)[0]
                if p[1] > args.precision_sure:
                    predicted = 1
                else:
                    predicted = 0
                if np.argmax(test_out[i]) == 1:
                    if predicted == 1:
                        tp += 1
                    else:
                        fp += 1
                else:
                    if predicted == 0:
                        tn += 1
                    else:
                        fn += 1

                print(" ".join(sent[i]), ins_ww[i], ins_cw[i], test_out[i], np.argmax(p), file=g)
        print('tp: {}, tn: {}, fp: {}, fn: {}', tp, tn, fp, fn)
        if (tp + fp) != 0 and (tp + fn) != 0:
            precision =  tp / (tp + fp)
            recall = tp / (tp + fn)
            beta = 0.5
            fscore = (1+beta**2) * ((precision * recall) / ((beta**2)*precision + recall))
        else:
            precision, recall, fscore = 0, 0, 0
        print('precision: {}, recall: {}, f1score: {}'.format(precision, recall, fscore))
        model.save(args.name + '.h5')
        
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Process some integers.')
    parser.add_argument('--small_run', dest='small_run', action='store_true', default=False)
    parser.add_argument('--name', dest="name", action="store", default="default")
    #parser.add_argument('--no_chars', dest="no_chars", action="store_true")
    parser.add_argument('--input_file', dest="input_file", action="store", default="infl.csv")
    parser.add_argument('--only_word', dest="only_word", action="store_true", default=False)
    parser.add_argument('--test_file', dest="test_file", action="store", default="test_precision.txt")
    parser.add_argument('--no_train', dest="no_train", action="store_true", default=False)
    parser.add_argument('--load', dest="load", action="store", default="infl_detect_all.h5")
    parser.add_argument('--precision_sure', dest="precision_sure", action="store", default=0.8, type=float)
    args = parser.parse_args()

    for k in args.__dict__:
        if args.__dict__[k] is not None:
            print(k, '->', args.__dict__[k])

    model = Model()
    model.run_model_rnn()
    