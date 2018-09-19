"""
Meta manager. Defines complex workflow in terms of lower level managers

For usage example see tests

"""
import re
from time import time
import logging

from collections import defaultdict, OrderedDict as odict
from copy import copy, deepcopy
from fractions import Fraction

import yaml

from shub_workflow.base import WorkFlowManager

from .utils import get_scheduled_jobs_specs


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


_STARTING_JOB_RE = re.compile("--starting-job(?:=(.+))?")


class GraphManager(WorkFlowManager):

    jobs_graph = {}
    base_failed_outcomes = ('failed', 'killed by oom', 'cancelled', 'cancel_timeout', 'memusage_exceeded',
                            'cancelled (stalled)')
    parallelization = None

    def __init__(self):
        self.__failed_outcomes = list(self.base_failed_outcomes)
        # Ensure jobs are traversed in the same order as they went pending.
        self.__pending_jobs = odict()
        self.__running_jobs = odict()
        self._available_resources = {}  # map resource : ammount
        self._acquired_resources = defaultdict(list)  # map resource : list of (job, ammount)
        self.__tasks = {}
        super(GraphManager, self).__init__()
        self.__start_time = time()
        for task in self.configure_workflow() or ():
            self._add_task(task)

    def _add_task(self, task):
        assert task.task_id not in self.jobs_graph, "Workflow inconsistency detected: task %s referenced twice." % task.task_id
        self.jobs_graph[task.task_id] = task.as_jobgraph_dict()
        self.__tasks[task.task_id] = task
        for ntask in task.get_next_tasks():
            self._add_task(ntask)

    def configure_workflow(self):
        pass

    def on_start(self):
        if not self.jobs_graph:
            self.argparser.error('Jobs graph configuration is empty.')
        if not self.args.starting_job and not self.args.resume_from_jobid:
            self.argparser.error('You must provide either --starting-job or --resume-from-jobid.')
        self._fill_available_resources()
        ran_tasks = self._maybe_setup_resume()
        self._setup_starting_jobs(ran_tasks)
        self.workflow_loop_enabled = True
        logger.info("Starting '%s' workflow", self.name)

    def _get_starting_jobs_from_resumed_job(self):
        starting_jobs = []
        job = self.project.jobs.get(self.args.resume_from_jobid)
        next_option_is_task = False
        for option in job.metadata.get('job_cmd'):
            if next_option_is_task:
                starting_jobs.append(option)
            else:
                m = _STARTING_JOB_RE.match(option)
                if m:
                    task = m.groups()[0]
                    if m:
                        starting_jobs.append(task)
                    else:
                        next_option_is_task = True
        return starting_jobs

    def _maybe_setup_resume(self):
        ran_tasks = []
        if self.args.resume_from_jobid:
            # fill tasks job ids
            logger.info("Will Resume from job (%s)", self.args.resume_from_jobid)
            for _, name, jobid in get_scheduled_jobs_specs(self, [self.args.resume_from_jobid]):
                mname, taskid = name.split('/')
                assert mname == self.name, "Resuming from wrong manager job: %s" % self.args.resume_from_jobid
                self.__tasks[taskid].append_jobid(jobid)
                ran_tasks.append(taskid)
        return ran_tasks

    def _setup_starting_jobs(self, ran_tasks, candidates=None):
        candidates = candidates or self.args.starting_job
        if not candidates:  # resuming
            candidates = self._get_starting_jobs_from_resumed_job()
        for taskid in candidates:
            if taskid in ran_tasks:
                logger.info("Task %s already done %s.", taskid, tuple(self.__tasks[taskid].get_scheduled_jobs()))
                next_tasks = [t.task_id for t in self.__tasks[taskid].get_next_tasks()]
                if next_tasks:
                    self._setup_starting_jobs(ran_tasks, next_tasks)
            else:
                self._add_initial_pending_job(taskid)
                logger.info("Resuming at task %s", taskid)

    def _fill_available_resources(self):
        """
        Ensure there are enough starting resources in order every job
        can run at some point
        """
        for job, job_info in self.jobs_graph.items():
            for required_resources in job_info.get('required_resources', []):
                for res_name, res_amount in required_resources.items():
                    old_amount = self._available_resources.get(res_name, 0)
                    if old_amount < res_amount:
                        logger.info("Increasing available resources count for %r"
                                    " from %r to %r.  Old value was not enough"
                                    " for job %r to run.",
                                    res_name, old_amount, res_amount, job)
                        self._available_resources[res_name] = res_amount

    def get_job(self, job, pop=False):
        if job not in self.jobs_graph:
            self.argparser.error('Invalid job: %s. Available jobs: %s' % (job, repr(self.jobs_graph.keys())))
        if pop:
            return self.jobs_graph.pop(job)
        return self.jobs_graph[job]

    def _add_initial_pending_job(self, job):
        wait_for = self.get_job(job).get('wait_for', [])
        self._add_pending_job(job, wait_for=tuple(wait_for))

    def _add_pending_job(self, job, wait_for=(), retries=0):
        basejobconf = self.get_job(job)
        wait_time = basejobconf.get('wait_time')
        required_resources_sets = basejobconf.get('required_resources', [])
        parallel_arg = self.get_job(job).pop('parallel_arg', None)
        if parallel_arg:
            # Split "parallelized" job into N parallel instances.
            del self.jobs_graph[job]
            for i in range(self.parallelization):
                parg = parallel_arg.replace('%d', '%d' % i)
                job_unit = "%s_%i" % (job, i)
                job_unit_conf = deepcopy(basejobconf)
                job_unit_conf['required_resources'] = []
                for required_resources in required_resources_sets:
                    job_unit_required_resources = {}
                    for res, res_amount in required_resources.items():
                        # Split required resource into N parts.  There are two
                        # ideas behind this:
                        #
                        # - if the job in whole requires some resources, each of
                        #   its N parts should be using 1/N of that resource
                        #
                        # - in most common scenario when 1 unit of something is
                        #   required, allocating 1/N of it means that when we start
                        #   one unit job, we can start another unit job to allocate
                        #   2/N, but not a completely different job (as it would
                        #   consume (1 + 1/N) of the resource.
                        #
                        # Use fraction to avoid any floating point quirks.
                        job_unit_required_resources[res] = Fraction(
                            res_amount, self.parallelization)
                    job_unit_conf['required_resources'].append(job_unit_required_resources)

                job_unit_conf.setdefault('init_args', []).append(parg)
                if 'retry_args' in job_unit_conf:
                    job_unit_conf['retry_args'].append(parg)
                for _, nextjobs in job_unit_conf.get('on_finish', {}).items():
                    if i != 0:  # only job 0 will conserve finish targets
                        for nextjob in copy(nextjobs):
                            if nextjob != 'retry':
                                if nextjob in self.jobs_graph:
                                    self.get_job(nextjob).setdefault('wait_for', []).append(job_unit)
                                    if nextjob in self.__pending_jobs:
                                        self.__pending_jobs[nextjob]['wait_for'].add(job_unit)
                                else:
                                    for i in range(self.parallelization):
                                        nextjobp = "%s_%i" % (job, i)
                                        self.get_job(nextjobp).get('wait_for', []).append(job_unit)
                                        if nextjobp in self.__pending_jobs:
                                            self.__pending_jobs[nextjobp]['wait_for'].add(job_unit)
                                nextjobs.remove(nextjob)
                self.jobs_graph[job_unit] = job_unit_conf
                self.__pending_jobs[job_unit] = {
                    'wait_for': set(wait_for),
                    'retries': retries,
                    # This field is only added for debug logging.
                    'required_resources': job_unit_conf.get('required_resources'),
                    'origin': job,
                    'wait_time': wait_time,
                }
            for other, oconf in self.jobs_graph.items():
                if job in oconf.get('wait_for', []):
                    oconf['wait_for'].remove(job)
                    if other in self.__pending_jobs:
                        self.__pending_jobs[other]['wait_for'].discard(job)
                    for i in range(self.parallelization):
                        job_unit = "%s_%i" % (job, i)
                        oconf['wait_for'].append(job_unit)
                        if other in self.__pending_jobs:
                            self.__pending_jobs[other]['wait_for'].add(job_unit)
        else:
            self.__pending_jobs[job] = {
                'wait_for': set(wait_for),
                'retries': retries,
                # This field is only added for debug logging.
                'required_resources': required_resources_sets,
                'wait_time': wait_time,
            }

    def add_argparser_options(self):
        super(GraphManager, self).add_argparser_options()
        self.argparser.add_argument('--jobs-graph', help='Define jobs graph_dict on command line', default='{}')
        self.argparser.add_argument('--starting-job', action='append', default=[],
                                    help='Set starting jobs. Can be given multiple times.')
        self.argparser.add_argument('--failed-outcomes', action='append', default=[],
                                    help='Add failed outcomes to the default ones. Can be given multiple times.')
        self.argparser.add_argument('--max-running-jobs', type=int,
                                    help='If given, don\'t allow more than the given jobs running at once. Useful'
                                         'for debug.')
        self.argparser.add_argument('--only-starting-jobs', action='store_true',
                                    help='If given, only run the starting jobs (don\'t follow on finish next jobs)')
        self.argparser.add_argument('--comment', help='Can be used for differentiate command line and avoid scheduling '
                                    'fail when a graph manager job is scheduled when another one with same option '
                                    'signature is running. Doesn\'t do anything else.')
        self.argparser.add_argument('--resume-from-jobid', help='Resume from the given graph manager jobid')

    def parse_args(self):
        args = super(GraphManager, self).parse_args()
        self.jobs_graph = yaml.load(args.jobs_graph) or deepcopy(self.jobs_graph)

        self.__failed_outcomes.extend(args.failed_outcomes)
        return args

    def workflow_loop(self):
        logger.debug("Pending jobs: %r", self.__pending_jobs)
        logger.debug("Running jobs: %r", self.__running_jobs)
        logger.debug("Available resources: %r", self._available_resources)
        logger.debug("Acquired resources: %r", self._acquired_resources)
        self.check_running_jobs()
        if self.__pending_jobs:
            self.run_pending_jobs()
        elif not self.__running_jobs:
            return False
        return True

    def get_command_line(self, job, retries):
        jobconf = self.get_job(job)
        command = jobconf['command']
        init_args = jobconf.get('init_args', [])
        retry_args = jobconf.get('retry_args', init_args)
        if retries:
            return [command] + retry_args
        return [command] + init_args

    def run_job(self, job, retries=False):
        task = self.__tasks.get(job)
        if task is not None:
            return task.run(self, retries)

        jobconf = self.get_job(job)
        hidden_args = jobconf.get('hidden_args')
        tags = jobconf.get('tags')
        units = jobconf.get('units')
        target_project_id = jobconf.get('project_id')
        version = "{}/{}".format(self.name, job)
        if retries:
            logger.info('Will retry job "%s/%s"', self.name, job)
        else:
            logger.info('Will start job "%s/%s"', self.name, job)
        cmd = self.get_command_line(job, retries)

        jobid = self.schedule_script(cmd, tags=tags, units=units, project_id=target_project_id)
        if jobid:
            logger.info('Scheduled job "%s/%s" (%s)', self.name, job, jobid)
            return jobid

    def _must_wait_time(self, job):
        conf = self.__pending_jobs[job]
        if conf['wait_time'] is not None:
            wait_time = conf['wait_time'] - time() + self.__start_time
            if wait_time > 0:
                logger.info("Job %s must wait %d seconds for running", job, wait_time)
                return True
        return False

    def run_pending_jobs(self):
        """Try running pending jobs.

        Normally, only jobs that have no outstanding dependencies are started.

        If all pending jobs have outstanding dependencies, try to start one job
        ignoring unknown tasks, i.e. those that are not currently pending.

        If none of the pending jobs cannot be started either way, it means
        there's a dependency cycle, in this case an error is raised.

        """

        # Normal mode: start jobs without dependencies.
        max_running_jobs = self.args.max_running_jobs or float('inf')
        for job in sorted(self.__pending_jobs.keys()):
            if len(self.__running_jobs) >= max_running_jobs:
                break
            conf = self.__pending_jobs[job]

            job_can_run = not conf['wait_for'] and not self._must_wait_time(job) and self._try_acquire_resources(job)
            if job_can_run:
                try:
                    jobid = self.run_job(job, conf['retries'])
                except:
                    self._release_resources(job)
                    raise
                self.__pending_jobs.pop(job)
                self.__running_jobs[job] = jobid

        if not self.__pending_jobs or self.__running_jobs or \
                any(conf['wait_time'] is not None for conf in self.__pending_jobs.values()):
            return

        # At this point, there are pending jobs, but none were started because
        # of dependencies, try "skip unknown deps" mode: start one job that
        # only has "unseen" dependencies to try to break the "stalemate."
        origin_job = None
        for job in sorted(self.__pending_jobs.keys()):
            if len(self.__running_jobs) >= max_running_jobs:
                break
            conf = self.__pending_jobs[job]
            job_can_run = (
                all(w not in self.__pending_jobs for w in conf['wait_for']) and
                (not origin_job or conf.get('origin') == origin_job) and
                self._try_acquire_resources(job))
            origin_job = conf.get('origin')
            if job_can_run:
                try:
                    jobid = self.run_job(job, conf['retries'])
                except:
                    self._release_resources(job)
                    raise
                self.__pending_jobs.pop(job)
                self.__running_jobs[job] = jobid
            if not origin_job and self.__running_jobs:
                return

        if self.__running_jobs:
            return

        # Nothing helped, all pending jobs wait for each other somehow.
        raise RuntimeError("Job dependency cycle detected: %s" % ', '.join(
            '%s waits for %s' % (
                job, sorted(self.__pending_jobs[job]['wait_for']))
            for job in sorted(self.__pending_jobs.keys())))

    def check_running_jobs(self):
        for job, jobid in list(self.__running_jobs.items()):
            outcome = self.is_finished(jobid)
            if outcome is not None:
                logger.info('Job "%s/%s" (%s) finished', self.name, job, jobid)
                for _, conf in self.__pending_jobs.items():
                    conf['wait_for'].discard(job)
                for _, conf in self.jobs_graph.items():
                    if job in conf.get('wait_for', []):
                        conf['wait_for'].remove(job)
                for nextjob in self._get_next_jobs(job, outcome):
                    if nextjob == 'retry':
                        jobconf = self.get_job(job)
                        retries = jobconf.get('retries', 0)
                        if retries > 0:
                            self._add_pending_job(job, retries=1)
                            jobconf['retries'] -= 1
                            logger.warning('Will retry job %s (outcome: %s)', job, outcome)
                    elif nextjob in self.__pending_jobs:
                        logger.error('Job %s already pending', nextjob)
                    else:
                        wait_for = self.get_job(nextjob).get('wait_for', [])
                        self._add_pending_job(nextjob, wait_for)
                self._release_resources(job)
                self.__running_jobs.pop(job)
            else:
                logger.info("Job %s (%s) still running", job, jobid)

    def _try_acquire_resources(self, job):
        result = True
        for resources in self.get_job(job).get('required_resources', []):
            for res_name, res_amount in resources.items():
                if self._available_resources[res_name] < res_amount:
                    result = False
                    break
            else:
                for res_name, res_amount in resources.items():
                    self._available_resources[res_name] -= res_amount
                    self._acquired_resources[res_name].append((job, res_amount))
                return True
        return result

    def _release_resources(self, job):
        for res_name, acquired in self._acquired_resources.items():
            for rjob, res_amount in acquired:
                if rjob == job:
                    self._available_resources[res_name] += res_amount
                    self._acquired_resources[res_name].remove((rjob, res_amount))

    def _get_next_jobs(self, job, outcome):
        if self.args.only_starting_jobs:
            return []
        on_finish = self.get_job(job).get('on_finish', {})
        if outcome in on_finish:
            nextjobs = on_finish[outcome]
        elif outcome in self.__failed_outcomes:
            nextjobs = on_finish.get('failed', [])
        else:
            nextjobs = on_finish.get('default', [])
        return nextjobs

    @property
    def pending_jobs(self):
        return self.__pending_jobs