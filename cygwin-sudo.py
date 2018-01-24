#!/usr/bin/env python3
import argparse
import errno
import fcntl
import os
import pty
import select
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import traceback
import tty
import concurrent.futures
from contextlib import ExitStack, closing, contextmanager

import termios

CMD_STDIN = 1
CMD_STDOUT = 2
CMD_STDERR = 3
CMD_WINSZ = 4
CMD_RETURN = 5


class PartialRead(Exception):
    pass


class MessageChannel:
    def __init__(self, sock):
        self.sock = sock

    def recv_n(self, n):
        d = []
        while n > 0:
            s = self.sock.recv(n)
            if not s:
                break
            d.append(s)
            n -= len(s)
        if n > 0:
            raise PartialRead('EOF while reading')
        return b''.join(d)

    def recv_message(self):
        length = struct.unpack('I', self.recv_n(4))[0]
        return self.recv_n(length)

    def recv_command(self):
        """Returns a tuple (cmd_type, data)"""
        message = self.recv_message()
        return struct.unpack('I', message[:4])[0], message[4:]

    def send_message(self, data):
        length = len(data)
        self.sock.send(struct.pack('I', length))
        self.sock.send(data)

    def send_command(self, cmd, data):
        self.send_message(struct.pack('I', cmd) + data)


class ElevatedServer:
    def main(self, argv):
        port = int(argv[1])
        password_file = argv[2]
        with open(password_file, 'rb') as f:
            password = f.read()

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        with closing(self.sock):
            self.sock.connect(('127.0.0.1', port))
            self.channel = MessageChannel(self.sock)
            received_password = self.channel.recv_message()
            if received_password != password:
                print("ERROR: invalid password")
                sys.exit(1)

            child_argv = self.channel.recv_message().split(b'\0')
            child_cwd = self.channel.recv_message()
            child_winsize = self.channel.recv_message()
            child_pty_flags = struct.unpack('bbb', self.channel.recv_message())
            env_packed = self.channel.recv_message()
            child_envdict = dict(x.split(b'=', 1) for x in env_packed.split(b'\0'))

            print("Elevated sudo server running:")
            print("> " + b" ".join(child_argv).decode())

            child_pid, child_fds = self.pty_fork(child_pty_flags)
            if child_pid == 0:
                self.child_fds = child_fds
                self.child_process(child_argv, child_cwd, child_winsize, child_envdict)
            else:
                self.child_pid = child_pid
                self.child_fds = child_fds
                self.main_process()

    def main_process(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            sf = executor.submit(self.sock_read_loop)
            cf = executor.submit(self.child_read_loop)

            concurrent.futures.wait([sf, cf], return_when=concurrent.futures.FIRST_COMPLETED)
            for fd in set(self.child_fds):
                os.close(fd)

            print('pty closed, getting return value')
            (success, exit_status) = os.waitpid(self.child_pid, 0)
            if not success or not os.WIFEXITED(exit_status):
                return_code = 1
                print('process did not shut down normally, no return value')
            else:
                return_code = os.WEXITSTATUS(exit_status)
                print('process finished with return value ', return_code)
            self.channel.send_command(CMD_RETURN, struct.pack('i', return_code))
            self.sock.shutdown(socket.SHUT_WR)

    def child_process(self, argv, cwd, winsize, envdict):
        try:
            os.chdir(cwd)
            if os.isatty(0):
                fcntl.ioctl(0, termios.TIOCSWINSZ, winsize)
            envdict[b'ELEVATED_SHELL'] = b'1'
            try:
                os.execvpe(argv[0], argv, envdict)
            except FileNotFoundError:
                print("sudo: Unknown command '{}'".format(os.fsdecode(argv[0])))
        except BaseException:
            traceback.print_exc()
        finally:
            os._exit(1)

    def child_read_loop(self):
        try:
            while True:
                for fd in select.select(self.child_fds[1:3], (), ())[0]:
                    chunk = os.read(fd, 8192)
                    if not chunk:
                        return
                    command = CMD_STDOUT if fd == self.child_fds[1] else CMD_STDERR
                    self.channel.send_command(command, chunk)
        except OSError:
            return
        except Exception as e:
            traceback.print_exc()
        finally:
            print('Child read loop terminated')

    def sock_read_loop(self):
        try:
            while True:
                cmd, data = self.channel.recv_command()
                if cmd == CMD_STDIN:
                    os.write(self.child_fds[0], data)
                elif cmd == CMD_WINSZ:
                    fcntl.ioctl(self.child_fds[1], termios.TIOCSWINSZ, data)
                    os.kill(self.child_pid, signal.SIGWINCH)
                else:
                    raise ValueError("Unexpected command:", cmd)
        except PartialRead:
            pass
        except Exception:
            traceback.print_exc()
        finally:
            print('Socket read loop terminated')

    def pty_fork(self, pty_flags):
        """Fork a child process, connecting to a new pty

        Args: pty_flags is a list of 3 booleans, specifying if the corresponding
              fd of the child should be connected to the pty
        Returns: a tuple (pid, fds): the fork-result pid and a list of the child's
                 3 standard streams. The child gets (0, None)"""

        pipes = [os.pipe() if not is_pty else None
                 for is_pty in pty_flags]
        if not pty_flags[0]:
            # STDIN goes the other direction
            pipes[0] = tuple(reversed(pipes[0]))

        has_pty = any(pty_flags)
        if has_pty:
            pid, child_pty = pty.fork()
        else:
            pid = os.fork()

        if pid == 0:
            for i, pipe in enumerate(pipes):
                if pipe:
                    os.dup2(pipe[1], i)

            return pid, None
        else:
            for pipe in pipes:
                if pipe:
                    os.close(pipe[1])
            return pid, [pipe[0] if pipe else child_pty for pipe in pipes]


class UnprivilegedClient:
    def main(self, command, window, **kwargs):
        password = os.urandom(32)
        with tempfile.NamedTemporaryFile("wb") as pwf:
            pwf.write(password)
            pwf.flush()
            listen_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listen_socket.bind(('127.0.0.1', 0))
            with closing(listen_socket):
                port = listen_socket.getsockname()[1]
                listen_socket.listen(1)

                try:
                    subprocess.check_call([
                        "cygstart", "--action=runas", window,
                        sys.executable, __file__,
                        '--elevated', 'server', str(port), pwf.name])
                except subprocess.CalledProcessError as e:
                    print("sudo: failed to start elevated process")
                    return

                listen_socket.settimeout(5)
                self.sock, acc = listen_socket.accept()
                self.channel = MessageChannel(self.sock)

            command_bytes = list(map(os.fsencode, command))
            self.run(password, command_bytes)

    def run(self, password, command):
        with closing(self.sock):
            self.channel.send_message(password)
            self.channel.send_message(b'\0'.join(command))
            self.channel.send_message(os.fsencode(os.getcwd()))
            self.channel.send_message(self.get_winsize())
            self.channel.send_message(struct.pack('bbb', os.isatty(0), os.isatty(1), os.isatty(2)))
            self.channel.send_message(b'\0'.join(b'%s=%s' % t for t in os.environb.items()))

            def handle_sigwinch(n, f):
                # TODO: fix race condition with normal send
                self.channel.send_command(CMD_WINSZ, self.get_winsize())

            signal.signal(signal.SIGWINCH, handle_sigwinch)

            with self.raw_term_mode():
                fdset = [0, self.sock.fileno()]
                while True:
                    for fd in select.select(fdset, (), ())[0]:
                        if fd == 0:
                            chunk = os.read(0, 8192)
                            if chunk:
                                self.channel.send_command(CMD_STDIN, chunk)
                            else:
                                # stdin is a pipe and is closed
                                fdset.remove(0)
                        else:
                            self.recv_command()

            self.sock.shutdown(socket.SHUT_WR)

    def recv_command(self):
        try:
            cmd, data = self.channel.recv_command()
        except PartialRead:
            print("sudo: Lost connection to elevated process")
            sys.exit(1)

        if cmd == CMD_STDOUT:
            os.write(1, data)
        elif cmd == CMD_STDERR:
            os.write(2, data)
        elif cmd == CMD_RETURN:
            sys.exit(struct.unpack('i', data)[0])
        else:
            raise ValueError("Unexpected message", cmd)

    @contextmanager
    def raw_term_mode(self):
        if not os.isatty(0):
            yield
        else:
            with ExitStack() as stack:
                attr = termios.tcgetattr(0)
                stack.callback(termios.tcsetattr, 0, termios.TCSAFLUSH, attr)

                def sighandler(n, f):
                    stack.close()
                    sys.exit(2)

                tty.setraw(0)
                for sig in (signal.SIGINT, signal.SIGTERM):
                    signal.signal(sig, sighandler)

                yield

    def get_winsize(self):
        if not os.isatty(0):
            return struct.pack('HHHH', 24, 80, 640, 480)

        winsz = struct.pack('HHHH', 0, 0, 0, 0)
        return fcntl.ioctl(0, termios.TIOCGWINSZ, winsz)


def main():
    parser = argparse.ArgumentParser(description="Run a command in elevated user mode")
    window_group = parser.add_mutually_exclusive_group()
    window_group.set_defaults(window='--hide')
    window_group.add_argument('--visible', action='store_const', dest='window',
                              const='--shownormal',
                              help="show the elevated console window")
    window_group.add_argument('--minimized', action='store_const', dest='window',
                              const='--showminnoactive',
                              help="show the elevated console window as a minimized window")
    parser.add_argument('--elevated', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('command', nargs=argparse.PARSER)
    args = parser.parse_args()

    if args.elevated:
        ElevatedServer().main(args.command)
    else:
        UnprivilegedClient().main(**vars(args))


if __name__ == '__main__':
    main()