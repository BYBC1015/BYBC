"""Own the durable Hub worker inside the desktop application's lifecycle."""
from pathlib import Path
import os
import threading
import time

from .agent_worker import main as worker_main


def default_agent_config():
    return Path(os.environ.get('APPDATA') or Path.home()) / 'CodexHubAgents' / 'config.json'


class AgentService:
    def __init__(self, config_path=None, *, log_callback=None, run_worker=None, retry_seconds=5):
        self.config_path=Path(config_path) if config_path else default_agent_config()
        self.log=log_callback or (lambda message: None)
        self.run_worker=run_worker or worker_main
        self.retry_seconds=retry_seconds
        self._lock=threading.RLock()
        self._stop=threading.Event()
        self._thread=None
        self._phase='stopped'
        self._last_contact=0.0
        self._device='-'

    @property
    def configured(self):
        return self.config_path.is_file()

    @property
    def is_running(self):
        return bool(self._thread and self._thread.is_alive())

    def start(self):
        with self._lock:
            if self.is_running: return
            if not self.configured:
                self.log('[Hub 智能体] 尚未配置执行连接。')
                return
            self._stop.clear()
            self._phase='starting'
            self._last_contact=0.0
            self._thread=threading.Thread(target=self._supervise,name='Hub-AgentService',daemon=True)
            self._thread.start()

    def stop(self):
        with self._lock:
            self._stop.set()
            if self.is_running:
                self._phase='stopping'
                self.log('[Hub 智能体] 停止接收新工作，正在等待已有工作安全收尾。')

    def _status(self, update):
        with self._lock:
            if update.get('connected'):
                reconnected = self._phase != 'running' and not self._stop.is_set()
                self._last_contact=time.monotonic()
                self._device=(update.get('device') or {}).get('label') or self._device
                if not self._stop.is_set(): self._phase='running'
                if reconnected: self.log('[Hub 智能体] 已收到 Hub 响应，执行连接在线。')
            elif update.get('external'):
                self._phase='external'
            elif update.get('phase'):
                self._phase=update['phase']

    def _supervise(self):
        delay=self.retry_seconds
        try:
            while not self._stop.is_set():
                try:
                    code=self.run_worker(['--config',str(self.config_path)],stop_event=self._stop,
                        status_callback=self._status,log_callback=lambda message:self.log('[Hub 智能体] '+message))
                    detail='退出码 '+str(code)
                except (Exception,SystemExit) as exc:
                    detail=type(exc).__name__+((' '+str(exc.code)) if isinstance(exc,SystemExit) and isinstance(exc.code,int) else '')
                if self._stop.is_set(): break
                with self._lock:
                    external=self._phase=='external'
                    if not external:self._phase='recovering'
                self.log('[Hub 智能体] '+('已有独立后台，等待接管。' if external else f'后台已退出（{detail}），{delay:g} 秒后恢复；执行收据保留。'))
                if self._stop.wait(delay): break
                delay=min(60,delay*2)
        finally:
            with self._lock:
                self._phase='stopped'
                self._last_contact=0.0

    def snapshot(self):
        with self._lock:
            alive=self.is_running
            online=alive and self._phase=='running' and 0<=time.monotonic()-self._last_contact<120
            label=('未配置' if not self.configured else '在线' if online else
                {'stopped':'已停止','starting':'正在连接','running':'连接中断 · 自动重试',
                 'stopping':'正在收尾','external':'独立后台运行 · 等待接管','recovering':'异常恢复中'}.get(self._phase,'状态待确认'))
            return {'id':'hub-agents','name':'Hub 智能体','backend':'Hub / Codex CLI','device':self._device,
                    'interval':3,'status':label,'online':online,'running':alive}
