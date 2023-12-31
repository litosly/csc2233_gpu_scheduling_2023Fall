import logging
import csv
import numpy as np
from matplotlib import pyplot as plt
import random
import tqdm

ALLOC_POLICY_DICT = {
    0: 'SJF',  # 'short job first', SJF
    1: 'SJU',  # SJF with estimator using USER feature
    2: 'SJG',  # SJF with estimator using GROUP, USER feature
    4: 'SJGG',  # SJF with estimator using GROUP, USER, GPU feature
    8: 'FIFO',  # FIFO, the default
    9: 'HRRN',  # Highest Response Ratio Next, Chooses the process with the highest ratio of the sum of its waiting time and its service time to its service time. Tends to balance short and long processes.
    10: 'HRRN_norm', # normalized HRRN 
    12: 'Lottery', # Lottery Sort
    13: 'FairShareGroup', # Fair Share Scheduling
    14: 'FairShareUser', # Fair Share by User instead of Group
    15: 'RL1', # RL algorithm 1, with reward = - np.sqrt(penalty_done_jobs) + 3 * throughput
    16: 'RL2', # RL algorithm 2, with reward = - np.sqrt(penalty_done_jobs)
    17: 'RL3', # RL algorithm 3, with reward = - average(np.sqrt(penalty_done_jobs)) + throughput
    18: 'RL4', # RL algorithm 4, with reward = - average(np.sqrt(penalty_wait_time_cluster_job)) + throughput
    19: 'RL5', # RL algorithm 5, with reward = - average(np.sqrt(penalty_wait_time_cluster_job)) + throughput
}

PREEMPT_POLICY_DICT = {
    0: 'SDF',  # 'smallest_duration_first'
    1: 'SSF',  # 'smallest_size_first
    2: 'LGF',  # 'large_gpu_first', # LGF, size:num_gpu
}

GPU_TYPE_INT_DICT = {
    "CPU":  0,
    "MISC": 1,
    "T4":   2,
    "P100": 3,
    "V100": 4
}


def print_fn(log, level=1):
    LOG_LEVEL_DEBUG = 0
    LOG_LEVEL_INFO = 1
    LOG_LEVEL_WARNING = 2
    LOG_LEVEL_ERROR = 3
    if level == LOG_LEVEL_DEBUG:
        logging.debug(log)
    elif level == LOG_LEVEL_INFO:
        logging.info(log)
    elif level == LOG_LEVEL_WARNING:
        logging.warning(log)
    elif level == LOG_LEVEL_ERROR:
        logging.error(log)
        exit()


def _repr_job_concise(job_dict):
    return "J %s([G %s,C %s]-D %s)" % (job_dict['job_id'], job_dict['num_gpu'], job_dict['num_cpu'], job_dict['duration'])


def _repr_job_preempt(job_dict):
    return "J %s-[G %s,C %s]-O:%3s/D:%3s" % (job_dict['job_id'], job_dict['num_gpu'], job_dict['num_cpu'], job_dict['on_time'], job_dict['duration'])


def _repr_job_done(job_dict):
    job_repr_concise = "J %s([G %s,C %s]-D %s-N %s)" % (job_dict['job_id'], job_dict['num_gpu'], job_dict['num_cpu'], job_dict['duration'], job_dict['node'])
    return "%25s: %4s ---> %4s" % (job_repr_concise, job_dict['jct'] - job_dict['duration'], job_dict['jct'])


def _add_describe(describe_file):
    if describe_file is None:
        return None
    describe_dict = {}
    with open(describe_file, 'r') as fd:
        reader = csv.DictReader(fd, delimiter=',')
        for row in reader:
            for k, v in row.items():
                if k=='count':
                    row[k] = int(v)
                elif k in ['mean', 'std', 'min', '25%', '50%', '75%', 'max']:
                    if v == '':
                        v = 0
                    row[k] = float(v)
            describe_dict[row['user']] = row
    return describe_dict  # dd['ae8ed1']['50%']==38.4 


def _add_job(job_list, job_dict, describe_dict=None):
    # Add job (job_dict) into job_list
    for key, value in job_dict.items():
        if value is not None and value.isdigit() and key != 'user':
            if type(value) == str:
                job_dict[key] = round(float(value))
            else:  # duration becomes an int
                job_dict[key] = round(value)
        elif key in ['wait_time','user_dur','user_gpu_dur','group_dur','group_gpu_dur']:
            try:
                job_dict[key] = float(value)
            except:
                pass

    keys = ['num_cpu', 'num_gpu', 'submit_time', 'num_inst']
    for key in keys:
        if key not in job_dict or job_dict[key] == '':
            if key in ['num_cpu', 'num_gpu']:
                job_dict[key] = 0
            else:  # key in ['submit_time', 'num_inst']
                job_dict[key] = 1
        else:
            if key in ['num_cpu', 'num_gpu']:  # in %
                job_dict[key] = round(100 * float(job_dict[key]))
            else:
                job_dict[key] = round(float(job_dict[key]))

    # Add entries to be used in scheduling
    job_dict['duration'] = int(float(job_dict['duration']))
    if job_dict['duration'] <= 0:
        job_dict['duration'] = 1  # fix duration == 0 problem.
    job_dict['size'] = int((job_dict['num_gpu'] + job_dict['num_cpu']) * job_dict['duration']) # (gpu + cpu) x duration
    job_dict['on_time'] = 0
    job_dict['wasted'] = 0
    job_dict['jct'] = -1
    job_dict['resource'] = [job_dict['num_gpu'], job_dict['num_cpu']] # list of resources
    job_dict['node'] = None

    # Add duration estimation
    if describe_dict is not None:
        jd_user = describe_dict.get(job_dict['user'])
        if jd_user is not None:
            job_dict['dur_avg'] = float(jd_user['mean'])  # expectation
            job_dict['dur_std'] = float(jd_user['std'])  # standard deviation
            job_dict['dur_med'] = float(jd_user['50%'])  # median
            job_dict['dur_trim_mean'] = float(jd_user['trim_mean'])  # discard 10% top and 10% tail when calc. mean

    # Remove original unused entries
    for drop_col in ['fuxi_job_name','fuxi_task_name','inst_id','running_cluster','model_name','iterations','interval','vc','jobid','status']:
        if drop_col in job_dict: job_dict.pop(drop_col)

    job_list.append(job_dict)


def add_user_round_robin_id(job_list):
    # Add a new sorting metrics, user_rrid, to enforce scheduler picking jobs from multiple users
    # when all users' primary metrics are the same (e.g., 0).
    user_rrid_dict = {}  # a new dict each time
    for job in job_list:
        user = job['user']
        rrid = user_rrid_dict.get(user, None)
        if rrid is None:
            rrid = 0
            user_rrid_dict[user] = 1
        else:
            user_rrid_dict[user] += 1
        job['user_rrid'] = rrid


def large_job_pruning(job_list, gpu_limit, cpu_limit):
    if job_list is None:
        return []
    for job in job_list:
        if 'num_gpu' in job and job['num_gpu'] > gpu_limit:
            gpu_was = job['num_gpu']
            job['num_gpu'] = gpu_limit
            print_fn("{:s}: GPU {:d} ==> {:d}".format(_repr_job_concise(job), gpu_was, gpu_limit))
        if 'num_cpu' in job and job['num_cpu'] > cpu_limit:
            cpu_was = job['num_cpu']
            job['num_cpu'] = cpu_limit
            print_fn("{:s}: CPU {:d} ==> {:d}".format(_repr_job_concise(job), cpu_was, cpu_limit))
    return job_list


def plot_cluster_util(npyfile, to_date=False):
    cluster_util = np.load(npyfile)
    cluster_time, cluster_cpu, cluster_gpu = cluster_util[0], cluster_util[1], cluster_util[2]

    plt.clf()
    plt.plot(cluster_time, cluster_cpu / 10, label='10CPU')
    plt.plot(cluster_time, cluster_gpu, label='GPU')
    plt.legend()
    try:
        plt.savefig(str(npyfile).split('.npy')[0]+".png")
    except:
        plt.savefig("cluster_util")


def plot_job_stats(npyfile, to_date=False):
    plt.figure(figsize=(16, 6), dpi=120)
    job_stats = np.load(npyfile)
    job_submit_time, job_duration, job_jct, job_gpu_type, job_num_inst, job_id = job_stats[0], job_stats[1], job_stats[2], job_stats[3], job_stats[4], job_stats[5]
    job_queue_delay = job_jct - job_duration

    plt.clf()
    plt.plot(job_submit_time, job_queue_delay, color='orange', label='queue_delay')
    plt.plot(job_submit_time, job_duration, color='black', alpha=0.3, label='duration')
    plt.legend()
    try:
        plt.savefig(str(npyfile).split('.npy')[0]+".png")
    except:
        plt.savefig("job_stats")


def plot_multi_job_stats(npyfiles, to_date=False):
    plt.clf()
    plt.figure(figsize=(12, 6), dpi=120)
    
    for npyfile in npyfiles:
        job_stats = np.load(npyfile)
        job_submit_time, job_duration, job_jct, job_gpu_type, job_num_inst, job_id = job_stats[0], job_stats[1], job_stats[2], job_stats[3], job_stats[4], job_stats[5]
        job_queue_delay = job_jct - job_duration

        try:
            label=ALLOC_POLICY_DICT[int(str(npyfile).split('.log.a')[1].split('-p')[0])]
        except KeyError:
            label = str(npyfile).split('.log.')[1].split('-job_stats.npy')[0]
        plt.plot(job_submit_time, job_queue_delay, alpha=0.5, label=label+'-queue_delay')
        plt.plot(job_submit_time, job_duration, color='grey', alpha=0.3, label='job duration')
    plt.legend(loc='upper left')
    plt.title("Arrival jobs' duration and queueing delay")
    plt.xlabel("Submitted Time")
    plt.ylabel("Run/Wait Time")
    try:
        plt.savefig(str(npyfile).split('.log.')[0]+"-job_stats.png")
    except:
        plt.savefig("job_stats")


def plot_multi_cluster_util(npyfiles, to_date=False):
    plt.clf()
    plt.figure(figsize=(12, 6), dpi=120)
    
    for npyfile in npyfiles:
        cluster_util = np.load(npyfile)
        cluster_time, cluster_cpu, cluster_gpu = cluster_util[0], cluster_util[1], cluster_util[2]

        try:
            label=ALLOC_POLICY_DICT[int(str(npyfile).split('.log.a')[1].split('-p')[0])]
        except KeyError:
            label = str(npyfile).split('.log.')[1].split('-cluster_util.npy')[0]
        plt.plot(cluster_time, cluster_gpu, alpha=0.5, label=label+'-GPU')
    plt.legend(loc='upper left')
    plt.title("Cluster Utilization")
    plt.xlabel("Time")
    plt.ylabel("Resource")
    try:
        plt.savefig(str(npyfile).split('.log.')[0]+"-cluster_util.png")
    except:
        plt.savefig("cluster_util")

def assign_tickets(job, weight_factor=1):
    """Assign tickets to a job based on its estimated duration."""
    return max(int(job['group_gpu_dur'] * weight_factor), 1)

def lottery_sort(job_list):
    """Efficient Lottery Scheduling algorithm to sort the job list in place."""
    # Assign tickets to each job and build a cumulative ticket sum list
    cumulative_tickets = []
    total_tickets = 0

    for job in job_list:
        job['tickets'] = assign_tickets(job)  # Ensure 'tickets' key is added to each job
        total_tickets += job['tickets']
        cumulative_tickets.append(total_tickets)

    for i in range(len(job_list)):
        if total_tickets <= 0:
            break  # Break the loop if there are no more tickets

        # Randomly select a ticket
        winning_ticket = random.randint(1, total_tickets)
        
        # Find the index of the job corresponding to the winning ticket
        winning_index = next(index for index, cumul in enumerate(cumulative_tickets) if cumul >= winning_ticket)

        # Swap the selected job with the job at the current position
        job_list[i], job_list[winning_index] = job_list[winning_index], job_list[i]

        # Update the cumulative tickets list and total tickets count
        if i != winning_index:
            diff = job_list[i]['tickets']
            for j in range(i, winning_index + 1):
                cumulative_tickets[j] -= diff
        total_tickets -= job_list[i]['tickets']


def fair_share_group(job_list):
    """
    Fair Share Scheduling algorithm to sort the job list for equal CPU time distribution among groups.
    """
    # Track CPU time used by each group
    gpu_time_used = {}

    # Initialize CPU time used for each group
    for job in job_list:
        group = job['group']
        if group not in gpu_time_used:
            gpu_time_used[group] = 0

    # Sort the job list in place based on the CPU time used by their respective groups
    job_list.sort(key=lambda job: gpu_time_used[job['group']])

    # Update CPU time used after scheduling (simulated)
    for job in job_list:
        group = job['group']
        gpu_time_used[group] += job['group_gpu_dur']

def fair_share_user(job_list):
    gpu_time_used = {}

    # Initialize CPU time used for each user
    for job in job_list:
        user = job['user']
        if user not in gpu_time_used:
            gpu_time_used[user] = 0

    # Sort the job list in place based on the CPU time used by each user
    job_list.sort(key=lambda job: gpu_time_used[job['user']])

    # Update CPU time used after scheduling (simulated)
    for job in job_list:
        user = job['user']
        gpu_time_used[user] += job['group_gpu_dur']
