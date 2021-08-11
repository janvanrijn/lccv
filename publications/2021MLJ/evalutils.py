import numpy as np
import pandas as pd
import openml
import lccv
import os, psutil
import gc
import logging

from func_timeout import func_timeout, FunctionTimedOut

import time
import random

import itertools as it
from scipy.sparse import lil_matrix

import sklearn
from sklearn import metrics
from sklearn import *

from func_timeout import func_timeout, FunctionTimedOut

def get_dataset(openmlid):
    """
    Reads in a dataset from openml.org via the ID, returning a matrix X and a label vector y.
    Discrete datasets are checked prior to dummy encoding on whether the encoding should be sparse.
    """
    ds = openml.datasets.get_dataset(openmlid)
    df = ds.get_data()[0].dropna()
    y = df[ds.default_target_attribute].values
    
    categorical_attributes = df.select_dtypes(exclude=['number']).columns
    expansion_size = 1
    for att in categorical_attributes:
        expansion_size *= len(pd.unique(df[att]))
        if expansion_size > 10**5:
            break
    
    if expansion_size < 10**5:
        X = pd.get_dummies(df[[c for c in df.columns if c != ds.default_target_attribute]]).values.astype(float)
    else:
        print("creating SPARSE data")
        dfSparse = pd.get_dummies(df[[c for c in df.columns if c != ds.default_target_attribute]], sparse=True)
        
        print("dummies created, now creating sparse matrix")
        X = lil_matrix(dfSparse.shape, dtype=np.float32)
        for i, col in enumerate(dfSparse.columns):
            ix = dfSparse[col] != 0
            X[np.where(ix), i] = 1
        print("Done. shape is" + str(X.shape))
    return X, y


def cv10(learner_inst, X, y, timeout=None, seed=None, r=None):
    return cv(learner_inst, X, y, 10, timeout, seed)

def cv5(learner_inst, X, y, timeout=None, seed=None, r=None):
    return cv(learner_inst, X, y, 5, timeout, seed)
    
def cv(learner_inst, X, y, folds, timeout, seed):
    deadline = None if timeout is None else time.time() + timeout
    kf = sklearn.model_selection.KFold(n_splits=10, random_state=np.random.RandomState(seed), shuffle=True)
    scores = []
    for train_index, test_index in kf.split(X):
        X_train, y_train = X[train_index], y[train_index]
        X_test, y_test = X[test_index], y[test_index]
        if deadline is None:
            learner_inst.fit(X_train, y_train)
        else:
            try:
                func_timeout(deadline - time.time(), learner_inst.fit, (X_train, y_train))
                y_hat = learner_inst.predict(X_test)
                error_rate = 1 - sklearn.metrics.accuracy_score(y_test, y_hat)
                scores.append(error_rate)
            except FunctionTimedOut:
                print(f"Timeout observed for 10CV, stopping and using avg of {len(scores)} folds.")
                break
            except KeyboardInterrupt:
                raise
            except:
                print("Observed some exception. Stopping")
                break
    return np.mean(scores) if scores else np.nan

def lccv90(learner_inst, X, y, r=1.0, timeout=None, seed=None): # maximum train size is 90% of the data (like for 10CV)
    try:
        return lccv.lccv(learner_inst, X, y, r=r, timeout=timeout, seed=seed)
    except KeyboardInterrupt:
        raise
    except:
        print("Observed some exception. Returning nan")
        return (np.nan,)

def lccv80(learner_inst, X, y, r=1.0, seed=None, timeout=None): # maximum train size is 80% of the data (like for 5CV)
    target_anchor = int(np.floor(X.shape[0] * 0.8))
    try:
        return lccv.lccv(learner_inst, X, y, r=r, timeout=timeout, seed=seed, target_anchor=target_anchor)
    except KeyboardInterrupt:
        raise
    except:
        print("Observed some exception. Returning nan")
        return (np.nan,)

def mccv(learner, X, y, target_size=None, r = 0.0, min_stages = 3, timeout=None, seed=0, repeats = 10):
    
    def evaluate(learner_inst, X, y, num_examples, seed=0, timeout = None, verbose=False):
        deadline = None if timeout is None else time.time() + timeout
        random.seed(seed)
        n = X.shape[0]
        indices_train = random.sample(range(n), num_examples)
        mask_train = np.zeros(n)
        mask_train[indices_train] = 1
        mask_train = mask_train.astype(bool)
        mask_test = (1 - mask_train).astype(bool)
        X_train = X[mask_train]
        y_train = y[mask_train]
        X_test = X[mask_test]
        y_test = y[mask_test]

        if verbose:
            print("Training " + str(learner_inst) + " on data of shape " + str(X_train.shape) + " using seed " + str(seed))
        if deadline is None:
            learner_inst.fit(X_train, y_train)
        else:
            func_timeout(deadline - time.time(), learner_inst.fit, (X_train, y_train))


        y_hat = learner_inst.predict(X_test)
        error_rate = 1 - sklearn.metrics.accuracy_score(y_test, y_hat)
        if verbose:
            print("Training ready. Obtaining predictions for " + str(X_test.shape[0]) + " instances. Error rate of model on " + str(len(y_hat)) + " instances is " + str(error_rate))
        return error_rate
    
    """
    Conducts a 90/10 MCCV (imitating a bit a 10-fold cross validation)
    """
    print("Running mccv with seed " + str(seed))
    train_size = 0.9
    if not timeout is None:
        deadline = time.time() + timeout
    
    scores = []
    n = X.shape[0]
    num_examples = int(train_size * n)
    
    seed *= 13
    for r in range(repeats):
        print("Seed in MCCV:",seed)
        if timeout is None:
            scores.append(evaluate(learner, X, y, num_examples, seed))
        else:
            try:
                if deadline <= time.time():
                    break
                scores.append(func_timeout(deadline - time.time(), evaluate, (learner, X, y, num_examples, seed)))
            except FunctionTimedOut:
                break

            except KeyboardInterrupt:
                break
                
            except:
                print("AN ERROR OCCURRED, not counting this run!")
        seed += 1

    return np.mean(scores) if len(scores) > 0 else np.nan, scores

def select_model(validation, learners, X, y, timeout_per_evaluation, epsilon, seed=0, exception_on_failure=False):
    validation_func = validation[0]
    validation_result_extractor = validation[1]
    
    hard_cutoff = 2 * timeout_per_evaluation
    r = 1.0
    best_score = 1
    chosen_learner = None
    validation_times = []
    exp_logger = logging.getLogger("experimenter")
    n = len(learners)
    for i, learner in enumerate(learners):
        learner_name = str(learner).replace("\n", " ")
        exp_logger.info(f"""
            --------------------------------------------------
            Checking learner {i + 1}/{n} ({learner_name})
            --------------------------------------------------""")
        exp_logger.info(f"Currently used memory: {int(psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024)}MB")
        try:
            validation_start = time.time()
            temp_pipe = clone(learner)
            score = validation_result_extractor(validation_func(temp_pipe, X, y, r = r, timeout=timeout_per_evaluation, seed=13 *seed + i))
            runtime = time.time() - validation_start
            validation_times.append(runtime)
            print(f"Observed score {score} for {temp_pipe}. Validation took {int(np.round(runtime * 1000))}ms")
            r = min(r, score + epsilon)
            print("r is now:", r)
            if score < best_score:
                best_score = score
                chosen_learner = temp_pipe
            else:
                del temp_pipe
                gc.collect()

        except KeyboardInterrupt:
            print("Interrupted, stopping")
            break
        except:
            if True or exception_on_failure:
                raise
            else:
                print("COULD NOT TRAIN " + str(learner) + " on dataset of shape " + str(X.shape) + ". Aborting.")
    return chosen_learner, validation_times

def evaluate_validators(validators, learners, X, y, timeout_per_evaluation, epsilon, seed=0, repeats=10):
    out = {}
    performances = {}
    for validator, result_parser in validators:
        
        print(f"""
        -------------------------------
        {validator.__name__} (with seed {seed})
        -------------------------------""")
        time_start = time.time()
        chosen_learner = select_model((validator, result_parser), learners, X, y, timeout_per_evaluation, epsilon, seed=seed)[0]
        runtime = int(np.round(time.time() - time_start))
        print("Chosen learner is " + str(chosen_learner) + ". Now computing its definitive performance.")
        if chosen_learner is None:
            out[validator.__name__] = ("n/a", runtime, np.nan)
        else:
            if not str(chosen_learner.steps) in performances:
                performances[str(chosen_learner.steps)] = mccv(chosen_learner, X, y, repeats=repeats, seed=4711)
            out[validator.__name__] = (chosen_learner.steps, runtime, performances[str(chosen_learner.steps)])
    return out