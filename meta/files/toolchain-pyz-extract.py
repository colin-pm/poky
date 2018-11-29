#! /usr/bin/env python3
#
# Copyright 2024 by Garmin Ltd. or its subsidiaries
# SPDX-License-Identifier: MIT
#
# /// script
# requires-python = ">=3.8"
# dependencies = []
# ///

import sys

if sys.hexversion < 0x30814F0:
    print("Python version 3.8 or later is required")
    sys.exit(1)

from contextlib import contextmanager
from pathlib import Path
import argparse
import glob
import io
import logging
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import zipfile

SDK_TITLE = "@SDK_TITLE@"
SDK_VERSION = "@SDK_VERSION@"
SDK_ARCH = "@SDK_ARCH@"
SDK_EXTENSIBLE = "@SDK_EXTENSIBLE@" == "1"
SDKPATH = "@SDKEXTPATH@" if SDK_EXTENSIBLE else "@SDKPATH@"
SELF_ZIP = Path(__file__).parent.absolute()
SDK_TAR_NAME = "@SDK_TAR_NAME@"
SDK_GCC_VER = "@SDK_GCC_VER@"
SDK_EXT_TYPE = "@SDK_EXT_TYPE@"
OLDEST_KERNEL = "@OLDEST_KERNEL@"
CHECKPOINT = 25600000
PRESERVED_ENVS = (
    "HOME",
    "TERM",
    "ICECC_PATH",
    "http_proxy",
    "https_proxy",
    "ftp_proxy",
    "no_proxy",
    "all_proxy",
    "GIT_PROXY_COMMAND",
)
PLATFORM_IS_WINDOWS = platform.system() == "Windows"


def prompt_bool(prompt, default=True):
    prompt = f"{prompt} [{"Y/n" if default else "y/N"}]"
    try:
        user_input = input(prompt)
    except EOFError:
        return default
    if not user_input:
        return default
    return user_input.lower().strip() == "y"


def environment_setup_scripts(destination):
    return sorted(
        glob.glob(os.path.join(glob.escape(destination), "environment-setup-*"))
    )


@contextmanager
def open_pyz(name, text=False):
    with zipfile.ZipFile(SELF_ZIP) as zip_file, zip_file.open(name) as fd:
        if not text:
            yield fd
        else:
            with io.TextIOWrapper(fd, encoding="utf-8") as wrapper:
                yield wrapper


@contextmanager
def sdk_archive():
    # The embedded tar file is opened as a read-only stream from inside the zip
    # file. This prevents it from having random access (e.g. the archive
    # members must be extracted in order), but is *much* faster
    with open_pyz(SDK_TAR_NAME) as sdk_tar, tarfile.open(
        mode="r|*", fileobj=sdk_tar
    ) as tf:
        yield tf


def run_command(*command, capture=False):
    logging.info("Running '%s'", " ".join(command))
    try:
        if capture:
            return subprocess.check_output(command, universal_newlines=True)
        subprocess.check_call(command)
    except subprocess.CalledProcessError as e:
        logging.info("Command failed with %d", e.returncode)
        raise e


@contextmanager
def run_script():
    script = tempfile.NamedTemporaryFile("w+", delete=False)
    try:
        script.write("#!/bin/sh\n")
        yield script
        script.seek(0)
        logging.debug("RUNNING SCRIPT:")
        for l in script:
            logging.debug("> %s" % l.rstrip())
        script.close()
        run_command("/bin/sh", script.name)
    finally:
        try:
            script.close()
        except:
            pass
        os.remove(script.name)


def ver_lteq(a, b):
    p = subprocess.Popen(
        ["sort", "-V"],
        stdout=subprocess.PIPE,
        stdin=subprocess.PIPE,
        universal_newlines=True,
    )
    stdout, _ = p.communicate("%s\n%s" % (a, b))
    return stdout.splitlines()[0] == a


def ver_lt(a, b):
    if a == b:
        return False
    return ver_lteq(a, b)


def get_gcc_version():
    output = run_command("gcc", "--version", capture=True)
    m = re.match(r".* ([0-9]+\.[0-9]+)\.[0-9]+.*", output)
    if m is not None:
        return m.group(1)


def get_kernel_version():
    return run_command("uname", "-r", capture=True).rstrip()


def translate_arch(arch):
    arch = re.sub(r"i[3-6]86", "ix86", arch)
    arch = re.sub(r"x86[-_]64", "x86_64", arch)
    arch = re.sub(r"AMD64", "x86_64", arch)
    return arch


def main():
    parser = argparse.ArgumentParser(description="Extract SDK")
    parser.add_argument(
        "--yes", "-y", help="Answer yes to all prompts", action="store_true"
    )
    parser.add_argument("--dir", "-d", help="Install SDK to DIR")
    debug_args = parser.add_argument_group(title="Advanced Debugging Only Options")
    debug_args.add_argument(
        "-S", dest="save_scripts", help="Save relocation scripts", action="store_true"
    )
    debug_args.add_argument(
        "-R", dest="relocate", help="Do not relocate executables", action="store_false"
    )
    debug_args.add_argument(
        "-D", dest="debug", help="Print debugging output", action="count", default=0
    )
    debug_args.add_argument(
        "-l",
        dest="list_files",
        help="List files that will be extracted",
        action="store_true",
    )
    ext_args = parser.add_argument_group(title="Extensible SDK Only Options")
    ext_args.add_argument(
        "-n",
        dest="prepare_buildsystem",
        help="Do not prepare build system",
        action="store_false",
    )
    ext_args.add_argument(
        "-p", dest="publish", help="Publish mode (implies -n)", action="store_true"
    )

    args = parser.parse_args()

    if args.debug >= 2:
        logging.getLogger().setLevel(logging.DEBUG)
    elif args.debug == 1:
        logging.getLogger().setLevel(logging.INFO)

    extract_exclude = set()

    if not PLATFORM_IS_WINDOWS:
        kernel_version = get_kernel_version()
        logging.info("Kernel version is %s", kernel_version)

        if ver_lt(kernel_version, OLDEST_KERNEL):
            print("Error: The SDK needs a kernel > %s" % OLDEST_KERNEL)
            return 1

    host_arch = translate_arch(platform.machine())
    logging.info("Host arch is %s", host_arch)
    sdk_arch = translate_arch(SDK_ARCH)

    if host_arch != sdk_arch:
        # Allow for installation of ix86 SDK on x86_64 host
        if host_arch != "x86_64" or sdk_arch != "ix86":
            print(
                f"Error: Incompatible SDK installer! Your host is {host_arch} and this SDK was built for {sdk_arch} hosts."
            )
            return 1

    if SDK_EXTENSIBLE:
        host_gcc_ver = get_gcc_version()
        if (
            (host_gcc_ver == "4.8" and SDK_GCC_VER == "4.9")
            or (host_gcc_ver == "4.8" and SDK_GCC_VER == "")
            or (host_gcc_ver == "4.9" and SDK_GCC_VER == "")
        ):
            print(
                "Error: Incompatible SDK installer! Your host gcc version is %s and this SDK was built by gcc higher version."
                % host_gcc_ver
            )
            return 1

        extract_exclude.add("ext-sdk-prepare.py")
        if SDK_EXT_TYPE == "minimal":
            extract_exclude.add("sstate-cache")

    title = f"{SDK_TITLE} installer version {SDK_VERSION}"
    print(title)
    print("=" * len(title))

    if not PLATFORM_IS_WINDOWS:
        with run_script() as script:
            script.write(f'DEFAULT_INSTALL_DIR="{SDKPATH}"\n')
            script.write('SUDO_EXEC=""\n')
            with open_pyz("pre_install_command", text=True) as pic:
                for l in pic:
                    script.write(l)

    if not args.dir:
        if args.yes:
            destination = SDKPATH
        else:
            try:
                destination = input(
                    "Enter target directory for SDK (default: %s): " % SDKPATH
                )
                if not destination:
                    destination = SDKPATH
            except EOFError:
                destination = SDKPATH
    else:
        destination = args.dir

    destination = os.path.realpath(os.path.abspath(destination))

    if len(destination) > 2048:
        print("Error: The target directory path is too long!!!")
        return 1

    if re.search(r"\s", destination):
        print(f"The target directory path ({destination}) contains whitespace. Abort!")
        return 1

    if os.path.exists(
        os.path.join(destination, "environment-setup-@REAL_MULTIMACH_TARGET_SYS@")
    ):
        print(
            f'The directory "{destination}" already contains a SDK for this architecture.'
        )
        if args.yes:
            proceed = True
        else:
            proceed = prompt_bool(
                "If you continue, existing files will be overwritten! Proceed?",
                default=False,
            )
    else:
        if args.yes:
            proceed = True
        else:
            proceed = prompt_bool(
                f'You are about to install the SDK to "{destination}". Proceed?'
            )

    if not proceed:
        print("Installation aborted!")
        return 1

    try:
        if args.list_files:
            with sdk_archive() as tf:
                tf.list()
            return 0

        print("Extracting SDK")
        logging.info("Making directory '%s'", destination)
        os.makedirs(destination, exist_ok=True)

        current_size = 0
        printed_checkpoints = 0
        try:

            def extract_file(tf, f):
                # If the destination exists and is not a directory, remove it.
                # This is done so that if the file is a symbolic link, it is
                # not followed when the member is extracted
                dest = os.path.join(destination, f.name)
                if os.path.lexists(dest) and (
                    os.path.islink(dest) or not os.path.isdir(dest)
                ):
                    logging.debug("Removing '%s'", dest)
                    os.unlink(dest)

                logging.debug("Extracting '%s'", dest)
                tf.extract(f, destination)
                nonlocal current_size
                current_size += f.size

                nonlocal printed_checkpoints
                num = (current_size // CHECKPOINT) - printed_checkpoints
                if num:
                    print("." * num, end="", flush=True)
                    printed_checkpoints += num

            with sdk_archive() as tf:
                for f in tf:
                    if any(s in f.name for s in extract_exclude):
                        continue
                    extract_file(tf, f)

        finally:
            print("")

    except PermissionError as e:
        if PLATFORM_IS_WINDOWS:
            to_try = "Administrative privileges"
        else:
            to_try = "'sudo'"
        print(f"Permission denied. Please try re-running with {to_try}:")
        print(e)
        return 1

    print("Setting it up...")

    def relocate_file(name):
        nonlocal destination

        is_real_env_script = False
        tmp_file = f"{name}.tmp"
        with open(name, "r") as f, open(tmp_file, "w") as tmp:
            logging.info("Relocating '%s'", name)
            for l in f:
                if "OECORE_NATIVE_SYSROOT=" in l:
                    # Handle custom env setup scripts that are only named
                    # environment-setup-* so that they have relocation applied.
                    # What we want beyond here is the main one rather than the
                    # one that simply sorts last
                    is_real_env_script = True

                l = l.replace(SDKPATH, destination)
                tmp.write(l)

        shutil.move(tmp_file, name)
        return is_real_env_script

    real_env_setup_script = None
    for s in environment_setup_scripts(destination):
        if relocate_file(s):
            real_env_setup_script = s

    if real_env_setup_script:
        env_setup_script = real_env_setup_script

    logging.info("Environment setup script is '%s'", env_setup_script)

    if not PLATFORM_IS_WINDOWS:
        with run_script() as script:
            script.write(f'DEFAULT_INSTALL_DIR="{SDKPATH}"\n')
            script.write('SUDO_EXEC=""\n')
            script.write(f'env_setup_script="{env_setup_script}"\n')
            script.write(f'target_sdk_dir="{destination}"\n')
            script.write(f"relocate={args.relocate}\n")
            script.write(f"savescripts={args.save_scripts}\n")
            with open_pyz("post_install_command", text=True) as pic:
                for l in pic:
                    script.write(l)

    # delete the relocating script, so that user is forced to re-run the
    # installer if he/she wants another location for the sdk
    if not args.save_scripts:
        for s in ("relocate_sdk.py", "relocate_sdk.sh"):
            try:
                p = os.path.join(os.path.dirname(env_setup_script), s)
                logging.info("Deleting '%s'", p)
                os.remove(p)
            except:
                pass

    # Execute post-relocation script
    post_relocate_script = os.path.join(destination, "post-relocate-setup.sh")
    if os.path.exists(post_relocate_script):
        relocate_file(post_relocate_script)
        if not PLATFORM_IS_WINDOWS:
            run_command("/bin/sh", post_relocate_script, destination, SDKPATH)
        if not args.save_scripts:
            os.remove(post_relocate_script)

    print("SDK has been successfully set up and is ready to be used.")
    print(
        "Each time you wish to use the SDK in a new shell session, you need to source the environment setup script e.g."
    )
    for script in environment_setup_scripts(destination):
        if PLATFORM_IS_WINDOWS:
            print(f" $ call {script}")
        else:
            print(f" $ . {script}")

    return 0


def check_env():
    if PLATFORM_IS_WINDOWS:
        return

    def tweakpath(p):
        if not f":{p}:" in os.environ["PATH"]:
            os.environ["PATH"] = f"{os.environ["PATH"]}:{p}"

    if not os.environ.get("ENVCLEANED"):
        args = ["-i", "ENVCLEANED=1", "LC_ALL=en_US.UTF8"]
        for e in PRESERVED_ENVS:
            args.append(f"{e}={os.environ.get(e, "")}")
        args.append(sys.executable)
        args.extend(sys.argv)

        os.execv("/usr/bin/env", args)

    os.environ["PATH"] = os.environ["PATH"].replace(":.", "").replace("::", "")

    tweakpath("/usr/sbin")
    tweakpath("/sbin")


if __name__ == "__main__":
    check_env()
    sys.exit(main())
