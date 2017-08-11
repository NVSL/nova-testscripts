#!/usr/bin/env python
import argparse
import pexpect
import sys
import re
import time
import logging as log
import StringIO
import subprocess
import fcntl
import os
import pwd
import collections

from JackalException import *
from XFSTests import XFSTests
from Runners import GCERunner, VMRunner

out = None

class Tee(object):
    def __init__(self, fs):
        self.files = fs
    def write(self, data):
        for i in self.files:
            i.write(data)
    def flush(self):
        for i in self.files:
            i.flush()

KernelConfig = collections.namedtuple("KernelConfig", "name kernel_repo kernel_config_file")
TestConfig = collections.namedtuple("TestConfig", "name tests test_class timeout")
NovaConfig = collections.namedtuple("NovaConfig", "name module_args kernel_repo kernel_config_file")

def main():

    parser = argparse.ArgumentParser()

    parser.add_argument("--prompt", default=None, help="prompt to watch for")
    parser.add_argument("--tests", default=None, nargs="*", help="which tests to run")
    parser.add_argument("--kernels", default=None, nargs="*", help="which kernel configs to run")
    parser.add_argument("--configs", default=None, nargs="*", help="which configurations to run")
    parser.add_argument("-v", default=False, action="store_true", help="be verbose")
    parser.add_argument("--runner", default="fixed", help="Run tests on GCE")
    parser.add_argument("--host", help="Runner to run on (for 'fixed')")

    parser.add_argument("--instance_prefix", help="Prefix used for runner instances")
    parser.add_argument("--reuse_image", default=False, action="store_true", help="If an existing image exists, use it")
    parser.add_argument("--dont_build_kernel", default=False, action="store_true", help="Don't build/install the kernel")
    parser.add_argument("--dont_reset", default=False, action="store_true", help="Don't reset the host between runs")
    parser.add_argument("--dont_prep", default=False, action="store_true", help="Don't prepare the host before starting")
    parser.add_argument("--dont_kill_runner", default=False, action="store_true", help="Don't kill the runner when finished")
    args = parser.parse_args()

    if not os.path.isdir("./results"):
        os.mkdir("./results")

    out = open("results/run_test.log", "w")

    if args.v:
        log.basicConfig(level=log.DEBUG)
        log.info("Being verbose")
        out=Tee([sys.stdout, out])
    else:
        log.basicConfig(level=log.INFO)

    if args.prompt is None:
        PROMPT = "{}@".format(pwd.getpwuid(os.getuid()).pw_name, args.host)
    else:
        PROMPT = args.prompt
        

    log.debug("Prompt = {}".format(PROMPT))

    
    def build_configs():
        config="""
        data_csum={data_csum}
        data_parity={data_parity}
        dram_struct_csum={dram_struct_csum}
        inplace_data_updates={inplace_data_updates}
        metadata_csum={metadata_csum}
        wprotect={wprotect}
        """
        r = []
        for data_csum in [0,1]:
            for data_parity in [0,1]:
                for dram_struct_csum in [0,1]:
                    for inplace_data_updates in [0,1]:
                        for metadata_csum in [0,1]:
                            for wprotect in [0,1]:
                                r.append(NovaConfig(name="baseline-{data_csum}-{data_parity}-{dram_struct_csum}-{inplace_data_updates}-{metadata_csum}-{wprotect}".format(**locals()),
                                                    kernel_repo=("https://github.com/NVSL/linux-nova.git", "HEAD"),
                                                    kernel_config_file="gce.v4.12.config",
                                                    module_args=config.format(**locals())))

        return r

    kernel_configs = [KernelConfig("nova-kernel",
                                   kernel_repo=("https://github.com/NVSL/linux-nova.git", "HEAD"),
                                   kernel_config_file="gce.v4.12.config")]

    all_configurations = build_configs()
    nova_configs = [NovaConfig(name="baseline",
                               kernel_repo=("https://github.com/NVSL/linux-nova.git", "HEAD"),
                               kernel_config_file="gce.v4.12.config",
                               module_args=""),
                    NovaConfig(name="baseline2",
                               kernel_repo=("https://github.com/NVSL/linux-nova.git", "HEAD"),
                               kernel_config_file="gce.v4.12.config",
                               module_args="wprotect=1")] + all_configurations

    tests = [TestConfig(name="xfstests1",
                        tests=["generic/092", "generic/080"],
                        timeout=100,
                        test_class=XFSTests),
             TestConfig(name="xfstests2",
                        tests=["generic/448", "generic/091"],
                        timeout=100,
                        test_class=XFSTests),
             TestConfig(name="xfstests-all",
                        tests=[], # this means all of them.
                        timeout=40*60,
                        test_class=XFSTests),
    ]

    if args.runner == "fixed":
        runner = VMRunner(args.host, PROMPT, args=args, log_out=out)
    elif args.runner == "gce":
        runner = GCERunner(PROMPT, args=args, prefix=args.instance_prefix, log_out=out)
        print "PROMPT={}".format(runner.prompt)
    else:
        raise JackalException("Illegal runner: {}".format(args.runner))
    
    def select(selection, universe, groups, default):    
        aliases = groups
        aliases.update({x.name: [x.name] for x in universe})
        aliases.update({None:[default]}) # the default

        if selection is None:
            selection = [default]
        r = []
        for i in selection:
            r += aliases[i]
        m = {x.name: x for x in universe}
        return [m[x] for x in sorted(list(set(r)))]

    kernels_to_run = select(args.kernels,
                            universe=kernel_configs,
                            groups={},
                            default="nova-kernel")

    nconfs_to_run = select(args.configs,
                           universe=nova_configs,
                           groups=dict(all=[x.name for x in all_configurations]),
                           default="baseline")
    
    tests_to_run = select(args.tests,
                          universe=tests,
                          groups={},
                          default="xfstests1")

    log.info("kernel_configs : " + " ".join([x.name for x in kernels_to_run]))
    log.info("nova_configs : " + " ".join([x.name for x in nconfs_to_run]))
    log.info("tests : " + " ".join([x.name for x in tests_to_run]))
    
    for kernel in kernels_to_run:
        try:
            runner.prepare_image(kernel, reuse=args.reuse_image) # update, build, and install the nova kernel
            for nconf in nconfs_to_run:
                try:
                    runner.create_instance(nconf, reuse=args.reuse_image)
                    first_time = True
                    
                    for tconf in tests_to_run:
                        test_name = "{}/{}/{}".format(kernel.name, nconf.name, tconf.name)
                        runner.prepare_instance(nconf, reboot=not first_time)
                        log.info("Running {}".format(test_name))

                        try:
                            test = tconf.test_class(test_name, tconf, runner)
                            test.go()
                        except JackalException as e:
                            log.error("{} failed: {}".format(test_name, e))
                        finally:
                            test.finish()
                            n = test_name.replace("/", "_")
                            with open("results/{}.junit".format(n), "w") as f:
                                f.write(test.junit)

                finally:
                    log.info("Cleaning up {}...".format(nconf.name))
                    if not args.dont_kill_runner:
                        runner.shutdown()
                        runner.delete()
        finally:
            # cleanup image creation
            pass

if __name__ == "__main__":
    main()
