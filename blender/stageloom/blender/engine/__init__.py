"""The engine: one tick per timer pass, per stack object.

Every stage is live: a delivered request is a solver step. A pass asks again
when the stage's own inputs or the frame move; an answer for a superseded
request is dropped, and a disconnected stage keeps its last output. A stage-less
file runs no timer or per-frame handlers. Nothing here touches the socket
directly: frames go through the bridge.

The session itself lives in :mod:`stageloom.blender.engine.runtime`: ``ENGINE``
is the only mutable owner, and every submodule imports it explicitly. This
module is the small public surface Blender registration, the operators, the
panel and the tests call; internal helpers live in their owning submodules.
"""

from .diagnostics import recent_activity as recent_activity
from .diagnostics import registration_report as registration_report
from .diagnostics import stage_timings as stage_timings
from .diagnostics import tick_rate as tick_rate
from .diagnostics import trigger_health as trigger_health
from .diagnostics import waiting_seconds as waiting_seconds
from .lifecycle import freeze as freeze
from .lifecycle import push as push
from .lifecycle import request_again as request_again
from .lifecycle import resolve_duplicate_key as resolve_duplicate_key
from .lifecycle import start_server as start_server
from .lifecycle import unfreeze as unfreeze
from .runtime import ENGINE as ENGINE
from .runtime import HOT_INTERVAL as HOT_INTERVAL
from .runtime import HOT_SECONDS as HOT_SECONDS
from .runtime import IDLE_INTERVAL as IDLE_INTERVAL
from .runtime import SOFT_RETRY_SECONDS as SOFT_RETRY_SECONDS
from .runtime import WAIT_INTERVAL as WAIT_INTERVAL
from .runtime import Engine as Engine
from .runtime import StageRuntime as StageRuntime


def register():
    ENGINE.register()


def unregister():
    ENGINE.unregister()
