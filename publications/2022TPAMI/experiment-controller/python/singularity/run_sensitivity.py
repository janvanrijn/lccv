import argparse
import resource
import typing

from evalutils import *
from lccv import lccv
from commons import *
import logging
import json


def parse_args():
    default_path = '~/experiments/lccv_sensitivity/'
    parser = argparse.ArgumentParser()
    parser.add_argument('--experiment_idx', required=True, type=int)
    parser.add_argument('--dataset_id', required=True, type=int)
    parser.add_argument('--timeout', type=int, default=300)
    parser.add_argument('--num_pipelines', type=int, default=200)
    parser.add_argument('--num_seeds', type=int, default=10)
    parser.add_argument('--folder', type=str, default=os.path.expanduser(default_path))
    parser.add_argument('--prob_dp', type=float, default=0.5)
    parser.add_argument('--prob_fp', type=float, default=0.5)
    return parser.parse_args()


def run_experiment(openmlid: int, num_pipelines: int, seed: int,
                   timeout: int, folder: str, prob_dp: float, prob_fp: float, config_map: typing.Dict):
    # TODO: built in check whether file already exists, in that case we can skipp
    # CPU
    print("CPU Settings:")
    for v in ["OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
              "BLIS_NUM_THREADS"]:
        print(f"\t{v}: {os.environ[v] if v in os.environ else 'n/a'}")

    # memory limits
    memory_limit = 14 * 1024
    print("Setting memory limit to " + str(memory_limit) + "MB")
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    resource.setrlimit(resource.RLIMIT_AS,
                       (memory_limit * 1024 * 1024, memory_limit * 1024 * 1024))

    # configure lccv logger (by default set to WARN, change it to DEBUG if tests fail)
    lccv_logger = logging.getLogger("lccv")
    lccv_logger.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    ch.setFormatter(formatter)
    lccv_logger.addHandler(ch)

    exp_logger = logging.getLogger("experimenter")
    exp_logger.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    ch.setFormatter(formatter)
    exp_logger.addHandler(ch)

    eval_logger = logging.getLogger("evalutils")
    eval_logger.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    ch.setFormatter(formatter)
    eval_logger.addHandler(ch)

    exp_logger.info("Starting python script")
    exp_logger.info(f"""Running experiment under following conditions:
    OpenML id: {openmlid}
    Seed: {seed}
    timeout (per single evaluation):  {timeout}
    Num Pipelines: {num_pipelines}
    Probability to draw a data-preprocessor: {prob_dp}
    Probability to draw a feature-preprocessor: {prob_fp}
    """)

    # CPU
    exp_logger.info("CPU Settings:")
    for v in ["OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS",
              "BLIS_NUM_THREADS"]:
        exp_logger.info(f"\t{v}: {os.environ[v] if v in os.environ else 'n/a'}")

    # memory limits
    memory_limit = 14 * 1024
    exp_logger.info("Setting memory limit to " + str(memory_limit) + "MB")
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    resource.setrlimit(resource.RLIMIT_AS,
                       (memory_limit * 1024 * 1024, memory_limit * 1024 * 1024))

    # load data
    binarize_sparse = openmlid in [1111, 41147, 41150, 42732, 42733]
    exp_logger.info(f"Reading dataset. Will be binarized sparsely: {binarize_sparse}")
    X, y = get_dataset(openmlid)
    exp_logger.info(f"ready. Dataset shape is {X.shape}, label column shape is {y.shape}. Now running the algorithm")
    if X.shape[0] <= 0:
        raise Exception("Dataset size invalid!")
    if X.shape[0] != len(y):
        raise Exception("X and y do not have the same size.")

    # creating learner sequence
    sampler = PipelineSampler("searchspace.json", X, y, seed, dp_proba=prob_dp,
                              fp_proba=prob_fp)
    test_learners = [sampler.sample(do_build=False) for i in
                     range(num_pipelines)]
    exp_logger.info(f"Evaluating portfolio of {len(test_learners)} learners.")

    # run lccv according to the settings of the sensitivity study
    epsilon = 0.0
    algorithm = "lccv-flex"
    train_size = 0.9
    final_repeats = 100
    
    selector = VerticalEvaluator(X, y, binarize_sparse, algorithm, train_size, timeout, epsilon = 0.01, seed=seed, other_args = config_map)
    
    # run selector
    time_start = time.time()
    model = selector.select_model(test_learners, errors="ignore")
    runtime = time.time() - time_start
    
    print("\n-------------------\n\n")
    
    
    if model is not None:
        
        exp_logger.info(f"Model selection phase over after {int(runtime)}s. Now determining final performance as average of {final_repeats} MCCV runs.")
        
        # compute validation performance of selection
        error_rates = selector.mccv(model, target_size=train_size, timeout=None, seed=seed, repeats = final_repeats)
        error_rates = [np.round(r, 4) for r in error_rates if not np.isnan(r)]
        error_rate = np.mean(error_rates)
        model_name = str(model).replace("\n", " ")
        exp_logger.info(f"""Run completed. Here are the details:
            Model: {model}
            Error Rate: {error_rate}
            Runtime: {runtime}
            {len(error_rates)}/{final_repeats} valid results in final evaluation: {np.array(error_rates)}""")
    else:
        exp_logger.info("No model was chosen. Assigning maximum error rate")
        error_rate = 1
        model_name = "None"
        
    # write result
    output = (model_name, np.nanmean(error_rate), error_rates, runtime)
    with open(folder + "/results.txt", "w") as outfile: 
        json.dump(output, outfile)
    exp_logger.info(f"Experiment ready. Results written to {folder}/results.txt")


def pipeline_args(index: int):
    if index < 3:  # 0, 1, 2 -> 2, 3, 4
        values = [2, 3, 4]
        hyperparameter = 'base'
        value = values[index]
    elif index < 11:  # [3-10]
        values = [-0.1, -.07, -.04, -.01, .01, .04, .07, .1]
        hyperparameter = 'MAX_ESTIMATE_MARGIN_FOR_FULL_EVALUATION'
        value = values[index-3]
    elif index < 13:  # [11, 12]
        values = [False, True]
        hyperparameter = 'use_train_curve'
        value = values[index-11]
    elif index < 17:  # 13, 14, 15, 16
        values = [0.10, 0.07, 0.04, 0.01]
        hyperparameter = 'max_conf_interval_size_default'
        value = values[index-13]
    elif index < 20:  # 17, 18, 19
        values = [4, 16, 64]
        hyperparameter = 'MAX_EVALUATIONS'
        value = values[index-17]
    elif index < 25:
        values = [1, 2, 3, 4, 5]
        hyperparameter = 'min_evals_for_stability'
        value = values[index - 20]
    else:
        raise ValueError('Illegal configuration index')
    return hyperparameter, value


def run_experiment_index_based(args):
    seed = args.experiment_idx % args.num_seeds
    hyperparameter, value = pipeline_args(int(np.floor(args.experiment_idx / args.num_seeds)))
    config_map = {hyperparameter: value, 'verbose': True}
    print(f"Config map: {config_map}")
    folder = os.path.expanduser(args.folder + '/lccv_sensitivity/%d/%s/%s/%d' % (args.dataset_id, hyperparameter, str(value), seed))
    os.makedirs(folder, exist_ok=True)
    run_experiment(args.dataset_id, args.num_pipelines, seed, args.timeout, folder, args.prob_dp, args.prob_fp, config_map)


if __name__ == '__main__':
    args = parse_args()
    print(f"Args: {args}")
    run_experiment_index_based(args)

