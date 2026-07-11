"""Persistent Kanata TCP command-channel lifecycle and fake-key output."""

from __future__ import annotations

import json
import queue
import select
import socket
import threading
import time


class KanataCommandRuntimeMixin:
    def _open_and_probe_tcp(self, timeout=4.0):
        deadline = time.perf_counter() + max(0.5, timeout)
        last_error = ""
        while time.perf_counter() < deadline:
            if not self.process or self.process.poll() is not None:
                return False, self.read_log() or "Kanata 在 TCP 就绪前已经退出"
            try:
                port = self.control_port
                if not port:
                    return False, "Kanata TCP 控制端口尚未分配"
                sock = socket.create_connection(
                    ("127.0.0.1", port), timeout=0.35
                )
                # Fake-key Press/Release and emergency messages are tiny. Nagle
                # can otherwise hold them for hundreds of milliseconds while
                # waiting for an ACK, which feels like input-dependent lag.
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.setblocking(False)
                with self.command_socket_lock:
                    self._close_command_socket_locked()
                    self.command_socket = sock
                    self.receive_buffer = b""
                self.last_command_error = ""
                self.fake_key_names_received = False
                self.available_fake_keys.clear()
                if not self._request_fake_key_names_now():
                    raise OSError(
                        self.last_command_error or "虚拟键清单请求发送失败"
                    )

                # RequestFakeKeyNames is a protocol-level health check. It proves
                # that this exact TCP server has loaded the current virtual-key
                # table without executing any keyboard or mouse action.
                probe_deadline = time.perf_counter() + 1.2
                while time.perf_counter() < probe_deadline:
                    self._drain_tcp_messages(0.05)
                    if self.last_command_error:
                        return False, self.last_command_error
                    if self.fake_key_names_received:
                        if "mchealth" not in self.available_fake_keys:
                            preview = ", ".join(
                                sorted(self.available_fake_keys)[:12]
                            ) or "无"
                            return False, (
                                "当前 Kanata 实例没有加载 MacroCanvas 的最新"
                                "虚拟键配置（缺少 mchealth）。已加载键："
                                f"{preview}"
                            )
                        return True, ""
                    with self.command_socket_lock:
                        if not self.command_socket:
                            break
                with self.command_socket_lock:
                    if self.command_socket:
                        return False, "Kanata 未返回虚拟键清单，TCP 启动检查超时"
            except OSError as error:
                last_error = str(error)
                self._close_command_socket()
            time.sleep(0.08)
        return False, (
            "无法连接本次 Kanata 进程的 TCP 控制端口 "
            f"127.0.0.1:{self.control_port or '未分配'}："
            f"{last_error or '等待超时'}"
        )

    def _start_command_worker(self):
        self.command_stop.clear()
        if self.command_thread and self.command_thread.is_alive():
            return
        self.command_thread = threading.Thread(
            target=self._command_worker,
            name=f"MacroCanvas-KanataTCP-{self.instance_name}",
            daemon=False,
        )
        self.command_thread.start()

    def _stop_command_worker(self, timeout=2.0):
        self.command_stop.set()
        try:
            self.command_queue.put_nowait(None)
        except queue.Full:
            pass
        # Closing the socket wakes any in-flight recv/select before joining.
        self._close_command_socket()
        thread = self.command_thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0.1, float(timeout)))
        alive = bool(thread and thread.is_alive())
        if alive:
            # The live worker still owns the queue. Do not drain it concurrently;
            # preserve both the thread reference and pending items for a retry.
            return False
        if self.command_thread is thread:
            self.command_thread = None
        while True:
            try:
                item = self.command_queue.get_nowait()
                if isinstance(item, dict):
                    item.get("result", {})["ok"] = False
                    item.get("result", {})["error"] = "Kanata 命令线程已停止"
                    done = item.get("done")
                    if done:
                        done.set()
                self.command_queue.task_done()
            except queue.Empty:
                break
        return True

    def _command_worker(self):
        current = threading.current_thread()
        try:
            while not self.command_stop.is_set():
                try:
                    # push-msg（预设触发、F8、全局开关）与输出命令共用此线程。
                    # 30ms 轮询会直接表现为触发延迟；缩短到 5ms，同时仍避免忙等。
                    item = self.command_queue.get(timeout=0.005)
                except queue.Empty:
                    self._drain_tcp_messages(0.0)
                    continue
                if item is None:
                    self.command_queue.task_done()
                    break

                done = item.get("done")
                result = item.get("result")
                try:
                    if item.get("probe"):
                        ok = self._confirm_server_processed_commands_now(
                            timeout=float(item.get("probe_timeout", 0.45))
                        )
                    elif item.get("barrier"):
                        ok = True
                    elif item.get("layer"):
                        ok = self._write_layer_now(
                            item["layer"],
                            confirm=bool(done),
                            timeout=float(item.get("confirm_timeout", 0.35)),
                        )
                    elif not self.is_running():
                        ok = False
                        self.last_command_error = "Kanata 尚未运行"
                    else:
                        ok = self._write_command_now(
                            item["name"], item["action"],
                            confirm_error=bool(done),
                        )
                        if not ok and self.is_running():
                            # Reconnect once. The first stream can be invalidated by
                            # a Kanata reload or by Windows network stack timing.
                            reconnect_ok, _ = self._open_and_probe_tcp(timeout=1.0)
                            if reconnect_ok:
                                ok = self._write_command_now(
                                    item["name"], item["action"],
                                    confirm_error=bool(done),
                                )
                        if ok:
                            with self.active_virtual_keys_lock:
                                if item["action"] == "Press":
                                    self.active_virtual_keys.add(item["name"])
                                elif item["action"] in ("Release", "Tap"):
                                    self.active_virtual_keys.discard(item["name"])
                                    self.quarantined_virtual_keys.discard(
                                        item["name"]
                                    )
                    if result is not None:
                        result["ok"] = bool(ok)
                        result["error"] = self.last_command_error
                except Exception as error:
                    self.last_command_error = f"Kanata 命令线程异常：{error}"
                    if result is not None:
                        result["ok"] = False
                        result["error"] = self.last_command_error
                finally:
                    if done:
                        done.set()
                    self.command_queue.task_done()
                    self._drain_tcp_messages(0.0)
        finally:
            if self.command_thread is current:
                self.command_thread = None

    def _request_fake_key_names_now(self):
        payload = (json.dumps({"RequestFakeKeyNames": {}}) + "\n").encode(
            "utf-8"
        )
        with self.command_socket_lock:
            sock = self.command_socket
            if not sock:
                self.last_command_error = "Kanata TCP 尚未连接"
                return False
            try:
                sock.sendall(payload)
                return True
            except OSError as error:
                self.last_command_error = f"Kanata TCP 发送失败：{error}"
                self._close_command_socket_locked()
                return False

    def _write_command_now(self, name, action, confirm_error=False):
        if action not in ("Press", "Release", "Tap", "Toggle"):
            self.last_command_error = f"不支持的 Kanata 虚拟键动作：{action}"
            return False
        if self.fake_key_names_received and name not in self.available_fake_keys:
            self.last_command_error = f"Kanata 配置中不存在虚拟键：{name}"
            return False

        # ActOnFakeKey has no success response.  Drain an older server error
        # before sending, then synchronously watch the short immediate-error
        # window for commands whose caller is waiting for a result.
        self._drain_tcp_messages(0.0)
        baseline_error_generation = self.tcp_error_generation
        self.last_command_error = ""
        payload = (
            json.dumps({
                "ActOnFakeKey": {
                    "name": name,
                    "action": action,
                }
            }, ensure_ascii=False)
            + "\n"
        ).encode("utf-8")
        with self.command_socket_lock:
            sock = self.command_socket
            if not sock:
                self.last_command_error = "Kanata TCP 尚未连接"
                return False
            try:
                sock.sendall(payload)
            except OSError as error:
                self.last_command_error = f"Kanata TCP 发送失败：{error}"
                self._close_command_socket_locked()
                return False
        if confirm_error:
            self._drain_tcp_messages(0.003)
            if self.tcp_error_generation != baseline_error_generation:
                return False
        return True

    def _write_layer_now(self, layer, confirm=False, timeout=0.35):
        requested_layer = str(layer)
        self._drain_tcp_messages(0.0)
        baseline_error_generation = self.tcp_error_generation
        self.last_command_error = ""
        payload = (
            json.dumps({"ChangeLayer": {"new": requested_layer}}) + "\n"
        ).encode("utf-8")
        with self.command_socket_lock:
            sock = self.command_socket
            if not sock:
                self.last_command_error = "Kanata TCP 尚未连接"
                return False
            try:
                sock.sendall(payload)
            except OSError as error:
                self.last_command_error = f"Kanata 层切换失败：{error}"
                self._close_command_socket_locked()
                return False
        if not confirm or self.current_layer == requested_layer:
            return True
        deadline = time.perf_counter() + max(0.05, float(timeout))
        while time.perf_counter() < deadline:
            self._drain_tcp_messages(0.01)
            if self.tcp_error_generation != baseline_error_generation:
                return False
            if self.current_layer == requested_layer:
                return True
        self.last_command_error = f"Kanata 未确认切换到层：{requested_layer}"
        return False

    def _drain_tcp_messages(self, wait_seconds=0.0):
        with self.command_socket_lock:
            sock = self.command_socket
        if not sock:
            return
        first_wait = max(0.0, wait_seconds)
        while True:
            try:
                readable, _, _ = select.select(
                    [sock], [], [], first_wait
                )
            except (OSError, ValueError):
                self._close_command_socket()
                return
            first_wait = 0.0
            if not readable:
                return
            try:
                chunk = sock.recv(4096)
            except BlockingIOError:
                return
            except OSError as error:
                self.last_command_error = f"Kanata TCP 接收失败：{error}"
                self._close_command_socket()
                return
            if not chunk:
                self.last_command_error = "Kanata TCP 连接已关闭"
                self._close_command_socket()
                return
            self.receive_buffer += chunk
            while b"\n" in self.receive_buffer:
                raw, self.receive_buffer = self.receive_buffer.split(b"\n", 1)
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    message = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict) and "Error" in message:
                    error = message.get("Error")
                    if isinstance(error, dict):
                        error = error.get("msg", error)
                    self.tcp_error_generation += 1
                    self.last_command_error = f"Kanata 拒绝执行命令：{error}"
                elif isinstance(message, dict) and "FakeKeyNames" in message:
                    self.fake_key_names_generation = int(getattr(
                        self, "fake_key_names_generation", 0
                    )) + 1
                    payload = message.get("FakeKeyNames")
                    if isinstance(payload, dict):
                        names = payload.get("names", [])
                    elif isinstance(payload, list):
                        names = payload
                    else:
                        names = []
                    self.available_fake_keys = {
                        str(name) for name in names if name is not None
                    }
                    self.fake_key_names_received = True
                elif isinstance(message, dict) and "MessagePush" in message:
                    payload = message.get("MessagePush")
                    if isinstance(payload, dict):
                        payload = payload.get("message", [])
                    if self.message_callback:
                        try:
                            self.message_callback(payload)
                        except Exception:
                            # TCP input processing must remain alive even if a
                            # UI callback is temporarily unavailable.
                            pass
                elif isinstance(message, dict) and "LayerChange" in message:
                    payload = message.get("LayerChange")
                    if isinstance(payload, dict):
                        layer = payload.get("new")
                        if layer:
                            self.current_layer = str(layer)

    def _close_command_socket_locked(self):
        sock = self.command_socket
        self.command_socket = None
        self.receive_buffer = b""
        self.fake_key_names_received = False
        self.available_fake_keys.clear()
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _close_command_socket(self):
        with self.command_socket_lock:
            self._close_command_socket_locked()

    def queue_virtual_key_action(
        self, name, action, wait=False, timeout=1.0
    ):
        if action not in ("Press", "Release", "Tap", "Toggle"):
            return False
        if self.fake_key_names_received and name not in self.available_fake_keys:
            self.last_command_error = f"Kanata 配置中不存在虚拟键：{name}"
            return False
        if (
            not self.is_running()
            or not self.command_thread
            or not self.command_thread.is_alive()
        ):
            return False
        done = threading.Event() if wait else None
        result = {} if wait else None
        self.command_queue.put({
            "name": name,
            "action": action,
            "done": done,
            "result": result,
        })
        if not wait:
            return True
        if not done.wait(max(0.05, timeout)):
            self.last_command_error = "等待 Kanata TCP 发送超时"
            return False
        return bool(result.get("ok"))

    def _confirm_server_processed_commands_now(self, timeout=0.45):
        """Confirm Kanata processed all prior commands on this TCP stream.

        Queue barriers only prove that Python wrote earlier commands to the
        socket.  RequestFakeKeyNames produces a response from Kanata itself;
        because the protocol is ordered, receiving that response proves every
        preceding Release was consumed before the engine may be terminated.
        """
        # Consume any older response before taking the generation baseline, so
        # a delayed reply from startup cannot falsely confirm this release batch.
        self._drain_tcp_messages(0.0)
        baseline = int(getattr(self, "fake_key_names_generation", 0))
        baseline_error = int(getattr(self, "tcp_error_generation", 0))
        if not self._request_fake_key_names_now():
            return False
        deadline = time.perf_counter() + max(0.05, float(timeout))
        while time.perf_counter() < deadline:
            self._drain_tcp_messages(0.01)
            if int(getattr(self, "tcp_error_generation", 0)) != baseline_error:
                return False
            if int(getattr(self, "fake_key_names_generation", 0)) > baseline:
                return True
            if not self.command_socket or not self.is_running():
                break
        self.last_command_error = "Kanata 未确认已处理释放命令"
        return False

    def flush_commands(self, timeout=1.0):
        if not self.command_thread or not self.command_thread.is_alive():
            return True
        done = threading.Event()
        result = {}
        self.command_queue.put({
            "probe": True,
            "probe_timeout": min(0.75, max(0.05, float(timeout) * 0.8)),
            "done": done,
            "result": result,
        })
        if not done.wait(max(0.05, timeout)):
            self.last_command_error = "等待 Kanata 释放确认超时"
            return False
        return bool(result.get("ok"))

    def change_layer(self, layer, wait=True, timeout=1.0):
        if (
            not self.is_running()
            or not self.command_thread
            or not self.command_thread.is_alive()
        ):
            return False
        done = threading.Event() if wait else None
        result = {} if wait else None
        self.command_queue.put({
            "layer": str(layer),
            "done": done,
            "result": result,
            # Leave queue/dispatch headroom inside the caller's total timeout.
            "confirm_timeout": min(0.75, max(0.05, float(timeout) * 0.8)),
        })
        if not wait:
            return True
        if not done.wait(max(0.05, timeout)):
            self.last_command_error = "等待 Kanata 层切换超时"
            return False
        if result.get("ok"):
            self.current_layer = str(layer)
        return bool(result.get("ok"))

    def release_all_virtual_keys(self, timeout=1.5, force=False):
        """Release every fake key whose successful Press has no matching Release."""
        with self.active_virtual_keys_lock:
            names = [
                name for name in self.active_virtual_keys
                if force or name not in self.quarantined_virtual_keys
            ]
        if not names or not self.is_running() or not self.command_thread:
            return True
        deadline = time.perf_counter() + max(0.2, timeout)
        success = True
        for name in reversed(names):
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                success = False
                break
            if not self.queue_virtual_key_action(
                name, "Release", wait=True, timeout=min(0.6, remaining)
            ):
                success = False
        flushed = self.flush_commands(
            timeout=max(0.1, deadline - time.perf_counter())
        )
        if flushed:
            # Let Kanata's output worker deliver the final KeyUp before a caller
            # immediately terminates the process.
            time.sleep(0.03)
        return bool(success and flushed)
