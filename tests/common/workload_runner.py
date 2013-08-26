#!/usr/bin/env python
# Copyright (c) 2012 Cloudera, Inc. All rights reserved.
#
# This module is used to run benchmark queries.  It runs the set queries specified in the
# given workload(s) under <workload name>/queries. This script will first try to warm the
# buffer cache before running the query. There is also a parameter to to control how
# many iterations to run each query.
import csv
import logging
import math
import os
import sys
import subprocess
import threading
from collections import defaultdict, deque
from optparse import OptionParser
from functools import partial
from os.path import isfile, isdir
from tests.common.query_executor import *
from tests.common.test_dimensions import *
from tests.common.test_result_verifier import *
from tests.util.calculation_util import calculate_median
from tests.util.test_file_parser import *
from time import sleep
from random import choice

# globals
WORKLOAD_DIR = os.environ['IMPALA_WORKLOAD_DIR']
IMPALA_HOME = os.environ['IMPALA_HOME']
PROFILE_OUTPUT_FILE = os.path.join(IMPALA_HOME, 'be/build/release/service/profile.tmp')
PRIME_CACHE_CMD = os.path.join(IMPALA_HOME, "testdata/bin/cache_tables.py") + " -q \"%s\""

dev_null = open(os.devnull)

logging.basicConfig(level=logging.INFO, format='%(threadName)s: %(message)s')
LOG = logging.getLogger('workload_runner')

class QueryExecutionDetail(object):
  def __init__(self, executor, workload, scale_factor, file_format, compression_codec,
               compression_type, execution_result):
    self.executor = executor
    self.workload = workload
    self.scale_factor = scale_factor
    self.file_format = file_format
    self.compression_codec = compression_codec
    self.compression_type = compression_type
    self.execution_result = execution_result


# Runs query files and captures results from the specified workload(s)
# The usage is:
# 1) Initialize WorkloadRunner with desired execution parameters.
# 2) Call workload_runner.run_workload() passing in a workload name(s) and scale
# factor(s).
# Internally, for each workload, this module looks up and parses that workload's
# query files and reads the workload's test vector to determine what combination(s)
# of file format / compression to run with. The queries are then executed
# and the results are displayed as well as saved to a CSV file.
class WorkloadRunner(object):
  def __init__(self, **kwargs):
    self.verbose = kwargs.get('verbose', False)
    if self.verbose:
      LOG.setLevel(level=logging.DEBUG)

    self.client_type = kwargs.get('client_type', 'beeswax')
    self.skip_impala = kwargs.get('skip_impala', False)
    self.compare_with_hive = kwargs.get('compare_with_hive', False)
    self.hive_cmd = kwargs.get('hive_cmd', 'hive -e ')
    self.target_impalads = deque(kwargs.get('impalad', 'localhost:21000').split(","))
    self.iterations = kwargs.get('iterations', 2)
    self.num_clients = kwargs.get('num_clients', 1)
    self.exec_options = kwargs.get('exec_options', str())
    self.prime_cache = kwargs.get('prime_cache', False)
    self.remote = not self.target_impalads[0].startswith('localhost')
    self.profiler = kwargs.get('profiler', False)
    self.use_kerberos = kwargs.get('use_kerberos', False)
    self.run_using_hive = kwargs.get('compare_with_hive', False) or self.skip_impala
    self.verify_results = kwargs.get('verify_results', False)
    self.plugin_runner = kwargs.get('plugin_runner', None)
    # TODO: Need to find a way to get this working without runquery
    #self.gprof_cmd = 'google-pprof --text ' + self.runquery_path + ' %s | head -n 60'
    self.__summary = str()
    self.__result_map = defaultdict(list)

  def get_next_impalad(self):
    """Maintains a rotating list of impalads"""
    self.target_impalads.rotate(-1)
    return self.target_impalads[-1]

  # Parse for the tables used in this query
  @staticmethod
  def __parse_tables(query):
    """
    Parse the tables used in this query.
    """
    table_predecessor = ['from', 'join']
    tokens = query.split(' ')
    tables = []
    next_is_table = 0
    for t in tokens:
      t = t.lower()
      if next_is_table == 1:
        tables.append(t)
        next_is_table = 0
      if t in table_predecessor:
        next_is_table = 1
    return tables

  def prime_remote_or_local_cache(self, query, remote, hive=False):
    """
    Prime either the local cache or buffer cache for a remote machine.
    """
    if remote:
      # TODO: Need to find what (if anything) we should do in the remote case
      return
    else:
      self.prime_buffer_cache_local(query)

  def prime_buffer_cache_local(self, query):
    """
    Prime the buffer cache on mini-dfs.

    We can prime the buffer cache by accessing the local file system.
    """
    # TODO: Consider making cache_tables a module rather than directly calling the script
    command = PRIME_CACHE_CMD % query
    os.system(command)

  def create_executor(self, db_name, executor_name, table_format_str, query_name):
    # Add additional query exec options here
    query_options = {
        'hive': lambda: (execute_using_hive,
          HiveQueryExecOptions(self.iterations,
          hive_cmd=self.hive_cmd,
          db_name=db_name,
          )),
        'impala_beeswax': lambda: (execute_using_impala_beeswax,
          ImpalaBeeswaxExecOptions(self.iterations,
          plugin_runner=self.plugin_runner,
          exec_options=self.exec_options,
          use_kerberos=self.use_kerberos,
          db_name=db_name,
          impalad=self.get_next_impalad(),
          table_format_str=table_format_str,
          query_name=query_name),
          ),
        'jdbc': lambda: (execute_using_jdbc,
          JdbcQueryExecOptions(self.iterations,
          impalad=self.get_next_impalad(),
          db_name=db_name)),
    } [executor_name]()
    return query_options

  def run_query(self, executor_name, db_name, query, prime_cache, exit_on_error,
      table_format_str, query_name):
    """
    Run a query command and return the result.

    Takes in a match functional that is used to parse stderr/out to extract the results.
    """
    if prime_cache:
      self.prime_remote_or_local_cache(query, self.remote, executor_name == 'hive')

    threads = []
    results = []

    output = None
    execution_result = None
    for client in xrange(self.num_clients):
      name = "Client Thread " + str(client)
      exec_tuple = self.create_executor(db_name, executor_name, table_format_str,
          query_name)
      threads.append(QueryExecutor(name, exec_tuple[0], exec_tuple[1], query,
          table_format_str, query_name))
    for thread in threads:
      LOG.debug(thread.name + " starting")
      thread.start()

    for thread in threads:
      thread.join()
      if not thread.success():
        if exit_on_error:
          LOG.error("Thread: %s returned with error. Exiting." % thread.name)
          raise RuntimeError, "Error executing query - '%s'. Aborting" \
            % thread.get_results().query_error
        else:
          LOG.error("Thread: %s returned with error - '%s'. Ignoring." % (thread.name,\
              thread.get_results().query_error))
      else:
        results.append(thread.get_results())
        LOG.debug(thread.name + " completed")
    # If all the threads failed, do not call __get_median_execution_result
    # and return a blank result.
    if not results: return None
    return self.__get_median_execution_result(results)

  def __get_median_execution_result(self, results):
    """
    Returns an ExecutionResult object whose avg/stddev is the median of all results.

    This is used when running with multiple clients to select a good representative value
    for the overall execution time.
    """
    # Choose a result to update with the mean avg/stddev values. It doesn't matter which
    # one, so just pick the first one.
    final_result = results[0]
    if len(results) == 1:
      return final_result
    final_result.avg_time = calculate_median([result.avg_time for result in results])
    if self.iterations > 1:
      final_result.std_dev = calculate_median([result.std_dev for result in results])
    return final_result

  @staticmethod
  def __enumerate_query_files(base_directory):
    """
    Recursively scan the given directory for all test query files.
    """
    query_files = list()
    for item in os.listdir(base_directory):
      full_path = os.path.join(base_directory, item)
      if isfile(full_path) and item.endswith('.test'):
        query_files.append(full_path)
      elif isdir(full_path):
        query_files += WorkloadRunner.__enumerate_query_files(full_path)
    return query_files

  @staticmethod
  def __extract_queries_from_test_files(workload):
    """
    Enumerate all the query files for a workload and extract the query strings.

    TODO: Update this to use the new test file parser
    """
    workload_base_dir = os.path.join(WORKLOAD_DIR, workload)
    if not isdir(workload_base_dir):
      raise ValueError,\
             "Workload '%s' not found at path '%s'" % (workload, workload_base_dir)

    query_dir = os.path.join(workload_base_dir, 'queries')
    if not isdir(query_dir):
      raise ValueError, "Workload query directory not found at path '%s'" % (query_dir)

    query_map = defaultdict(list)
    for query_file_name in WorkloadRunner.__enumerate_query_files(query_dir):
      LOG.debug('Parsing Query Test File: ' + query_file_name)
      sections = parse_query_test_file(query_file_name)
      test_name = re.sub('/', '.', query_file_name.split('.')[0])[1:]
      for section in sections:
        query_map[test_name].append((section['QUERY_NAME'],
                                     (section['QUERY'], section['RESULTS'])))
    return query_map

  def execute_queries(self, query_map, workload, scale_factor, query_names,
                      stop_on_query_error, test_vector):
    """
    Execute the queries for combinations of file format, compression, etc.

    The values needed to build the query are stored in the first 4 columns of each row.
    """
    # TODO : Find a clean way to get rid of globals.
    file_format, data_group, codec, compression_type = [test_vector.file_format,
        test_vector.dataset, test_vector.compression_codec, test_vector.compression_type]

    executor_name = self.client_type
    # We want to indicate this is IMPALA beeswax (currently dont' support hive beeswax)
    executor_name = 'impala_beeswax' if executor_name == 'beeswax' else executor_name

    query_name_filter = None
    if query_names:
      query_name_filter = [name.lower() for name in query_names.split(',')]
    LOG.info("Running Test Vector - File Format: %s Compression: %s / %s" %\
        (file_format, codec, compression_type))
    for test_name in query_map.keys():
      for query_name, query_and_expected_result in query_map[test_name]:
        query, results = query_and_expected_result
        if not query_name:
          query_name = query
        if query_name_filter and (query_name.lower() not in query_name_filter):
          LOG.info("Skipping query '%s'" % query_name)
          continue

        db_name = QueryTestSectionReader.get_db_name(test_vector, scale_factor)
        query_string = QueryTestSectionReader.build_query(query.strip(), test_vector, '')
        table_format_str = '%s/%s/%s' % (file_format, codec, compression_type)
        self.__summary += "\nQuery (%s): %s\n" % (table_format_str, query_name)
        execution_result = QueryExecutionResult()
        if not self.skip_impala:
          self.__summary += " Impala Results: "
          LOG.debug('Running: \n%s\n' % query_string)
          if query_name != query:
            LOG.info('Query Name: \n%s\n' % query_name)

          execution_result = self.run_query(executor_name, db_name, query_string,
                                            self.prime_cache, stop_on_query_error,
                                            table_format_str, query_name)

          # Don't verify insert results and allow user to continue on error if there is
          # a verification failure
          if execution_result is not None and\
             self.verify_results and 'insert' not in query.lower():
            try:
              verify_results(results.split('\n'), execution_result.data,
                             contains_order_by(query))
            except AssertionError, e:
              if stop_on_query_error:
                raise
              LOG.error(e)
          if execution_result:
            self.__summary += "%s\n" % execution_result

        hive_execution_result = QueryExecutionResult()
        if self.compare_with_hive or self.skip_impala:
          self.__summary += " Hive Results: "
          hive_execution_result = self.run_query('hive', db_name,
                                                         query_string,
                                                         self.prime_cache,
                                                         False, table_format_str,
                                                         query_name)
          if hive_execution_result:
            self.__summary += "%s\n" % hive_execution_result
        LOG.debug("---------------------------------------------------------------------")

        execution_detail = QueryExecutionDetail(executor_name, workload, scale_factor,
            file_format, codec, compression_type, execution_result)

        hive_execution_detail = QueryExecutionDetail('hive', workload, scale_factor,
            file_format, codec, compression_type, hive_execution_result)

        self.__result_map[(query_name, query)].append((execution_detail,
                                                       hive_execution_detail))

  def get_summary_str(self):
    return self.__summary

  def get_results(self):
    return self.__result_map

  def run_workload(self, workload, scale_factor=str(), table_formats=None,
                   query_names=None, exploration_strategy='core',
                   stop_on_query_error=True):
    """
      Run queries associated with each workload specified on the commandline.

      For each workload specified in, look up the associated query files. Extract valid
      queries in each file and execute them using the specified number of execution
      iterations. Finally, write results to an output CSV file for reporting.
    """
    LOG.info('Running workload: %s / Scale factor: %s' % (workload, scale_factor))
    query_map = WorkloadRunner.__extract_queries_from_test_files(workload)

    test_vectors = None
    if table_formats:
      table_formats = table_formats.split(',')
      dataset = get_dataset_from_workload(workload)
      test_vectors =\
          [TableFormatInfo.create_from_string(dataset, tf) for tf in table_formats]
    else:
      test_vectors = [vector.value for vector in\
          load_table_info_dimension(workload, exploration_strategy)]

    args = [query_map, workload, scale_factor, query_names, stop_on_query_error]
    execute_queries_partial = partial(self.execute_queries, *args)
    map(execute_queries_partial, test_vectors)
