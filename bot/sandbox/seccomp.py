"""Seccomp BPF filter for the workspace sandbox (ctypes over libseccomp).

Denylist, default-allow: the syscalls responsible for the bulk of the
unprivileged kernel-LPE surface (io_uring, bpf, userfaultfd, perf_event_open,
the kernel keyring, ptrace, mount/namespace manipulation, module loading, ...)
fail with EPERM, while the broad POSIX surface arbitrary Python code
legitimately uses stays untouched. An allowlist would be stronger on paper but
brittle in production: community-member scripts pull in numpy/matplotlib-class
libraries whose syscall footprint shifts release to release, and a false
positive here surfaces as an inexplicable tool failure. libseccomp's generated
program kills any process whose syscall arch differs from the native one
(the SCMP_FLTATR_ACT_BADARCH default is SCMP_ACT_KILL), so 32-bit/x32 side
doors around the native deny rules are closed without enumerating them.

The program is built once per process via the system ``libseccomp.so.2``
(loaded with ctypes, so no Python dependency; the library is effectively
universal on Linux since systemd links it) and handed to bubblewrap as raw
cBPF bytes on a memfd (``bwrap --seccomp FD``), which installs it right before
exec'ing the payload. If the library is missing or the build fails,
:func:`open_bpf_fd` raises, ``sandbox_available()`` returns False, and the
code-exec tools are simply not registered, failing closed like every other
layer of the profile.

Names that do not resolve on the running arch (e.g. ``vm86`` outside x86) are
skipped; the unit tests pin the critical names to guard against typos silently
weakening the list.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import functools
import os
import tempfile

_SCMP_ACT_ALLOW = 0x7FFF0000
_SCMP_ACT_ERRNO_EPERM = 0x00050000 | errno.EPERM

#: Syscalls denied inside the sandbox (EPERM). Grouped by why they are on the
#: list; everything here is useless to legitimate workspace scripts.
DENIED_SYSCALLS: tuple[str, ...] = (
    # Kernel module loading / boot & accounting control.
    "init_module",
    "finit_module",
    "delete_module",
    "kexec_load",
    "kexec_file_load",
    "reboot",
    "acct",
    "syslog",
    # eBPF, perf, tracing: the top recurring sources of kernel LPEs, and
    # cross-process introspection on the shared uid outside the pid namespace.
    "bpf",
    "perf_event_open",
    "ptrace",
    "process_vm_readv",
    "process_vm_writev",
    "kcmp",
    "lookup_dcookie",
    # io_uring: large, fast-moving attack surface with a steady CVE history.
    "io_uring_setup",
    "io_uring_enter",
    "io_uring_register",
    # Kernel keyring: keys persist per-uid beyond the sandbox's lifetime.
    "add_key",
    "request_key",
    "keyctl",
    # Page-fault interception and cross-process fd/memory grabs.
    "userfaultfd",
    "process_madvise",
    "pidfd_getfd",
    "memfd_secret",
    # Mount and filesystem-namespace manipulation (bwrap's mount table is
    # final), including the classic open_by_handle_at container escape.
    "mount",
    "umount",
    "umount2",
    "pivot_root",
    "chroot",
    "open_tree",
    "move_mount",
    "fsopen",
    "fsconfig",
    "fsmount",
    "fspick",
    "mount_setattr",
    "open_by_handle_at",
    "name_to_handle_at",
    # Namespace creation/entry beyond what bwrap already set up (userns is
    # already hard-disabled; this closes the remaining CLONE_NEW* surface).
    "setns",
    "unshare",
    # Host/system administration; capabilities already block these, seccomp
    # keeps them unreachable even to a capability-confused kernel path.
    "sethostname",
    "setdomainname",
    "swapon",
    "swapoff",
    "quotactl",
    "quotactl_fd",
    "nfsservctl",
    "settimeofday",
    "clock_settime",
    "clock_adjtime",
    "adjtimex",
    "fanotify_init",
    "fanotify_mark",
    # NUMA policy: historic LPE surface, meaningless inside the sandbox.
    "mbind",
    "move_pages",
    "migrate_pages",
    "set_mempolicy",
    "get_mempolicy",
    # Obscure execution-domain and port-I/O switches.
    "personality",
    "modify_ldt",
    "iopl",
    "ioperm",
    "vm86",
    "vm86old",
    "uselib",
    "_sysctl",
    "ustat",
    "sysfs",
)


class SeccompUnavailableError(RuntimeError):
    """The seccomp filter cannot be built on this host (fail closed)."""


def _load_libseccomp() -> ctypes.CDLL:
    name = ctypes.util.find_library("seccomp") or "libseccomp.so.2"
    try:
        lib = ctypes.CDLL(name, use_errno=True)
    except OSError as exc:
        raise SeccompUnavailableError(f"libseccomp could not be loaded: {exc}") from exc
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_init.argtypes = (ctypes.c_uint32,)
    # int seccomp_rule_add(ctx, action, syscall, arg_cnt, ...) is always called
    # with arg_cnt=0, so the variadic tail is never populated.
    lib.seccomp_rule_add.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint)
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_syscall_resolve_name.argtypes = (ctypes.c_char_p,)
    lib.seccomp_export_bpf.restype = ctypes.c_int
    lib.seccomp_export_bpf.argtypes = (ctypes.c_void_p, ctypes.c_int)
    lib.seccomp_release.restype = None
    lib.seccomp_release.argtypes = (ctypes.c_void_p,)
    return lib


@functools.lru_cache(maxsize=1)
def seccomp_bpf_bytes() -> bytes:
    """Build the deny-list filter and return it as raw cBPF program bytes.

    Cached for the process lifetime (the list is static). Raises
    :class:`SeccompUnavailableError` when libseccomp is missing or any step
    fails; exceptions are not cached, so a transient failure is retried.
    """
    lib = _load_libseccomp()
    ctx = lib.seccomp_init(_SCMP_ACT_ALLOW)
    if not ctx:
        raise SeccompUnavailableError("seccomp_init failed")
    try:
        for name in DENIED_SYSCALLS:
            num = lib.seccomp_syscall_resolve_name(name.encode("ascii"))
            if num < 0:
                # Not a syscall on the native arch (negative pseudo-number,
                # e.g. vm86 outside x86); nothing to deny here.
                continue
            rc = lib.seccomp_rule_add(ctx, _SCMP_ACT_ERRNO_EPERM, num, 0)
            if rc != 0:
                raise SeccompUnavailableError(f"seccomp_rule_add({name}) failed: {rc}")
        with tempfile.TemporaryFile() as tmp:
            rc = lib.seccomp_export_bpf(ctx, tmp.fileno())
            if rc != 0:
                raise SeccompUnavailableError(f"seccomp_export_bpf failed: {rc}")
            tmp.seek(0)
            data = tmp.read()
    finally:
        lib.seccomp_release(ctx)
    # struct sock_filter is 8 bytes; a sane program is a non-empty multiple.
    if not data or len(data) % 8 != 0:
        raise SeccompUnavailableError(f"exported BPF program is malformed ({len(data)} bytes)")
    return data


def _anonymous_fd() -> int:
    """An fd on an anonymous read-write file: memfd, or an unlinked temp file.

    Some standalone CPython builds ship without the ``os.memfd_create``
    binding even though the kernel supports it; a dup'd ``TemporaryFile``
    (already unlinked) has the same lifetime semantics.
    """
    if hasattr(os, "memfd_create"):
        return os.memfd_create("sandbox-seccomp-bpf")
    with tempfile.TemporaryFile() as tmp:
        return os.dup(tmp.fileno())


def open_bpf_fd() -> int:
    """Return a read-positioned fd holding the filter, for ``bwrap --seccomp``.

    Each launch gets its own fd (offsets are per open file description, so
    concurrent launches must not share one). The caller owns the fd and must
    close it once the child has been spawned.
    """
    data = seccomp_bpf_bytes()
    fd = _anonymous_fd()
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.lseek(fd, 0, os.SEEK_SET)
    except BaseException:
        os.close(fd)
        raise
    return fd
