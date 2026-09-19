"""Match the `sync key:` lines printed by lua/bot_controlled.lua.tpl back to the actions we sent.

Every action dict carries a unique `extraData` string. When lua executes the action it prints
    sync key: <extraData> <RealTime> ### <DotaTime> ### <step>
to the console log, which lets us measure action latency and detect dropped actions.
"""
import os
import time


def monitor_log(console_log_path, pattern_queue, result_queue, max_wait=1.0):
    """Child-process target. Put {"pattern": str, "dotatime": float} in, get latency dicts out.

    Requests without "dotatime" just wait (forever) for a line containing the pattern and return it.
    """
    while not os.path.isfile(console_log_path):
        time.sleep(0.5)

    latest_lua_step = -1
    latest_lua_realtime = -1

    with open(console_log_path, 'r', encoding="UTF-8", errors="replace") as f:
        while True:
            request = pattern_queue.get()
            pattern = request["pattern"]
            during_game = 'dotatime' in request
            start_time = time.time()

            while True:
                if during_game and time.time() - start_time > max_wait:
                    result_queue.put({'reaction_time': -1, 'lua_realtime': -1, 'lua_step': -1, 'error_code': 0})
                    break

                position = f.tell()
                line = f.readline()
                if not line:
                    f.seek(position)
                    time.sleep(0.001)
                    continue
                if pattern not in line:
                    continue
                if not during_game:
                    result_queue.put(line)
                    break

                try:
                    _, _, lua_realtime, lua_dotatime, lua_step = line.split("###")
                    lua_realtime, lua_dotatime, lua_step = float(lua_realtime), float(lua_dotatime), int(lua_step)
                except ValueError:
                    result_queue.put({'reaction_time': 0, 'lua_realtime': 0, 'lua_step': 0, 'error_code': 1})
                    break

                first = latest_lua_step == -1
                result_queue.put({
                    'reaction_time': int((lua_dotatime - request["dotatime"]) * 1000),
                    'lua_realtime': 0 if first else int((lua_realtime - latest_lua_realtime) * 1000),
                    'lua_step': 0 if first else lua_step - latest_lua_step,
                    'error_code': 0,
                })
                latest_lua_realtime = lua_realtime
                latest_lua_step = lua_step
                break
