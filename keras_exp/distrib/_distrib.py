'''
'''
from __future__ import print_function

import sys
from collections import OrderedDict
# from contextlib import contextmanager

from abc import ABCMeta, abstractproperty
import warnings

import tensorflow as tf


__all__ = ('JobType', 'DevType', 'TFClusterManagerFacade',)


ABC = ABCMeta('ABC', (object,), {})  # compatible with Python 2 *and* 3


# @six.add_metaclass(ABCMeta)
class ClusterParser(ABC):
    '''ClusterParser Abstract Base Class. Defines the interface expected
    of a cluster parser.
    '''

    @abstractproperty
    def num_tasks_per_host(self):
        '''List of integers. Length of list is number of hosts. Each list
        element specifies number of tasks on the host. A corresponding
        property hostnames is a list of hosts with each element specifying
        the host name. This list and hostnames list must be in the same order.
        '''

    @abstractproperty
    def hostnames(self):
        '''List of hosts with each element specifying the host name.'''

    @abstractproperty
    def num_parameter_servers(self):
        '''Number of parameter servers to create/use in the cluster.'''

    @abstractproperty
    def my_proc_id(self):
        '''Current process's id or rank.'''


class JobType(object):
    worker = 'worker'
    ps = 'ps'  # parameter-server


class DevType(object):
    cpu = 'cpu'
    gpu = 'gpu'


class TFClusterManagerFacade(object):
    '''
    Setting config on ther server instantiation and then re-using this same
    config for sesssions is very important. This functionality is wrapped
    in TFClusterManagerFacade.
    '''

    def __init__(self, num_tasks_per_host, hostnames,
                 num_parameter_servers, my_proc_id, starting_port=2222):
        num_processes = sum(num_tasks_per_host)
        # tuples of (str(Hostname:Port), JobName, TaskID) for each process
        proc_info = [[None, None, None] for _ in range(num_processes)]

        # Assign Port# to each process according to Hostname
        # Note: if there are multiple processes on the same hostname,
        # each one needs it's own port number, hence the variable name
        # starting_port)
        pid = 0
        first_pid_per_host = {}  # Reverse-Lookup map
        for cnt, hostname in zip(num_tasks_per_host, hostnames):
            first_pid_per_host[hostname] = pid
            for i in range(cnt):
                proc_info[pid][0] = "{}:{}".format(
                    hostname, starting_port + i)
                pid += 1

        # Assign PSs to different physical hosts
        # NOTE: this code requires that the num_parameter_servers be less than
        # or equalto the number of indificial physical nodes
        ps_strings = []
        for ps_id in range(num_parameter_servers):
            pid = first_pid_per_host[hostnames[ps_id]]
            ps_strings.append(proc_info[pid][0])
            proc_info[pid][1] = JobType.ps
            proc_info[pid][2] = ps_id

        # Assign workers to the remaining open spots
        wk_id = 0
        wk_strings = []
        for info in proc_info:
            if info[1] is None:  # It's not a ps
                wk_strings.append(info[0])
                info[1] = JobType.worker
                info[2] = wk_id
                wk_id += 1

        # Each processor: Grab your Job/TaskID
        self._myhost = proc_info[my_proc_id][0].split(':')[0]
        self._myjobtype = proc_info[my_proc_id][1]
        self._mytask_id = proc_info[my_proc_id][2]

        # Retain the overall cluster definition.
        self._cspec_dict = {JobType.worker: wk_strings, JobType.ps: ps_strings}

    @property
    def is_chief(self):
        task_id = self.mytask_id
        # Worker with task id 0 is chief
        is_chief = (task_id == 0) and (self.myjobtype == JobType.worker)
        return is_chief

    @property
    def myhost(self):
        return self._myhost

    @property
    def myjobtype(self):
        return self._myjobtype

    @property
    def mytask_id(self):
        return self._mytask_id

    @property
    def clusterspec_dict(self):
        return self._cspec_dict

    def get_cluster_spec(self):
        return tf.train.ClusterSpec(self.clusterspec_dict)

    def get_server(self, config=None, protocol=None, start=True):
        '''In distributed environment with multi-GPUs per node, set config
        gpus option to allow growth.
            config.gpu_options.allow_growth = True
        '''
        if not config.gpu_options.allow_growth:
            warnings.warn('Set config.gpu_options.allow_growth=True '
                          'to avoid allocation errors on Multi-GPU nodes',
                          UserWarning)
        cspec = self.get_cluster_spec()
        server = tf.train.Server(cspec, job_name=self.myjobtype,
                                 task_index=self.mytask_id,
                                 config=config,
                                 protocol=protocol, start=start)
        return server

    # @contextmanager
    def get_session(self, server):
        '''TF session getter. Works  as context manager directly as well.'''
        config = server.server_def.default_session_config
        # with tf.Session(server.target, config=config) as sess:
        #     yield sess  # force usage of context manager
        return tf.Session(server.target, config=config)

    def join(self, server, sess=None, exit_flag=True):
        # server.join()
        task_id = self.mytask_id
        jobtype = self.myjobtype

        mydevtask = tf.DeviceSpec(job=jobtype, task=task_id)
        queue = create_done_queue_task(mydevtask)

        if sess is None:
            # config = server.server_def.default_session_config
            # with tf.Session(server.target, config=config) as sess:
            with self.get_session(server) as sess:
                sess.run(queue.dequeue())
        else:
            sess.run(queue.dequeue())

        print("{} {} RECEIVED DONE. QUITTING".format(jobtype, task_id),
              file=sys.stderr)

        if exit_flag:
            sys.exit(0)

    def stop_chief(self, server, sess=None):
        # num_ps = cluster_spec.num_tasks(JobType.ps)
        # num_workers = cluster_spec.num_tasks(JobType.worker)
        num_ps = len(self.clusterspec_dict[JobType.ps])
        num_workers = len(self.clusterspec_dict[JobType.worker])
        enq_ops = []

        ps_devtasklist = [tf.DeviceSpec(job=JobType.ps, task=ii)
                          for ii in range(num_ps)]
        wrk_devtasklist = [tf.DeviceSpec(job=JobType.worker, task=ii)
                           for ii in range(1, num_workers)]
        devtasklist = ps_devtasklist + wrk_devtasklist
        for q in create_done_queues_chief(devtasklist):
            qop = q.enqueue(1)
            enq_ops.append(qop)

        if sess is None:
            # config = server.server_def.default_session_config
            # with tf.Session(server.target, config=config) as sess:
            with self.get_session(server) as sess:
                for op in enq_ops:
                    sess.run(op)
        else:
            for op in enq_ops:
                sess.run(op)

    def get_workers_dev_list(self, ngpus):
        '''Current split strategy is if 1 worker on a node then all GPUs are
        assigned to that worker. If more than 1 worker then 1 GPU per worker.
        '''
        # TODO: GPU TO WORKERS MAPPING STRATEGY
        #     1 GPU PER WORKER
        #     SPLIT M GPUs PER N WORKERS M > N: M/N GPUs per WORKER

        # The ngpus per host needs to be done with MPI or somehow sync'd.
        # Currently assuming all hosts have the same number of GPUs.

        # workers_list = cluster_spec.job_tasks(JobType.worker)
        workers_list = self.clusterspec_dict[JobType.worker]
        task_id = self.mytask_id

        workers_nodetask_map = OrderedDict()
        for itask, worker in enumerate(workers_list):
            wnode = worker.split(':')[0]  # keep the hostname and discard port
            workers_nodetask_map.setdefault(wnode, []).append(itask)

        # print('WORKERS_NODETASK_MAP: {}'.format(workers_nodetask_map))  #
        # DEBUG

        mywgdev = None  # This probably should be the task name without device.
        wgdev_list = []
        # TODO: Generalize this as cluster spec split strategy.
        for itask_list in workers_nodetask_map.values():
            ntasks_per_node = len(itask_list)
            if ntasks_per_node > 1:
                # 1 GPU per worker on a node. 1 WORKER PER CPU-CORE
                # Woker Tasks within a Node
                for itask_cnt, itask in enumerate(itask_list):
                    # USE CPUS for extra workers
                    devtype, devid = (DevType.gpu, itask_cnt) \
                        if itask_cnt < ngpus else \
                        (DevType.cpu, itask_cnt - ngpus)
                    wgdev = tf.DeviceSpec(
                        job=JobType.worker, task=itask, device_type=devtype,
                        device_index=devid)

                    wgdev_list.append(wgdev)

                    if task_id == itask:
                        mywgdev = wgdev

            elif ntasks_per_node == 1 and ngpus > 0:
                # ALL GPUs per worker on a node. 1 WORKER PER CPU-CORE
                itask = itask_list[0]
                for idev in range(ngpus):
                    wgdev = tf.DeviceSpec(
                        job=JobType.worker, task=itask,
                        device_type=DevType.gpu, device_index=idev)

                    wgdev_list.append(wgdev)

                if task_id == itask:
                    # No particular device id since multiple GPUs per task.
                    mywgdev = tf.DeviceSpec(job=JobType.worker, task=task_id,
                                            device_type=DevType.gpu)

            elif ntasks_per_node == 1:
                itask = itask_list[0]
                # USE CPUS
                wgdev = tf.DeviceSpec(
                    job=JobType.worker, task=itask, device_type=DevType.cpu,
                    device_index=0)

                wgdev_list.append(wgdev)

                if task_id == itask:
                    mywgdev = wgdev

            else:
                continue

        return mywgdev, wgdev_list


# =============================================================================
# SIGNAL QUEUES: https://github.com/hustcat/tensorflow_examples/blob/master/mnist_distributed/dist_fifo.py @IgnorePep8
# =============================================================================
# def create_done_queue(i, num_workers=1):
#     """Queue used to signal death for i'th ps shard. Intended to have
#       all workers enqueue an item onto it to signal doneness."""
#
#     with tf.device("/job:ps/task:%d" % (i)):
#         return tf.FIFOQueue(num_workers, tf.int32,
#                             shared_name="done_queue{}".format(i))
#
#
# def create_done_queues(num_ps):
#     return [create_done_queue(i) for i in range(num_ps)]

# Perhaps implement a READY queue just like DONE queues.


def create_done_queue_task(dev):
    '''
    :param dev: Device spec.
    :type dev: :class:`tf.DeviceSpec`
    '''
    task_id = dev.task
    with tf.device(dev):
        shared_name = 'done_queue_chief_{}'.format(task_id)
        return tf.FIFOQueue(1, tf.int32, shared_name=shared_name)


def create_done_queues_chief(devlist):
    '''
    :param devlist: List of device specs :class:`tf.DeviceSpec` objects.
    '''
    return [create_done_queue_task(dev) for dev in devlist]
