"""Bounded QMP audio commands, executed inside one owned container."""
import json
import socket
import sys
import time


def monitor(commands):
    with socket.create_connection(('127.0.0.1', 7200), timeout=5) as connection:
        deadline = time.monotonic() + 15
        with connection.makefile('rwb') as stream:
            def receive():
                if time.monotonic() >= deadline:
                    raise TimeoutError('QMP audio command deadline exceeded')
                connection.settimeout(max(0.001, deadline - time.monotonic()))
                line = stream.readline(65537)
                if not line or len(line) > 65536:
                    raise RuntimeError('Invalid QMP audio response')
                return json.loads(line)
            if 'QMP' not in receive():
                raise RuntimeError('Missing QMP greeting')
            results = []
            requests = [{'execute': 'qmp_capabilities'}] + [
                {'execute': 'human-monitor-command', 'arguments': {'command-line': command}}
                for command in commands]
            for index, payload in enumerate(requests):
                payload['id'] = index
                stream.write((json.dumps(payload) + '\n').encode())
                stream.flush()
                while True:
                    reply = receive()
                    if 'event' in reply:
                        continue
                    if reply.get('id') != index or 'error' in reply or 'return' not in reply:
                        raise RuntimeError('QMP audio command failed: ' + repr(reply))
                    results.append(reply['return'])
                    break
            return results[1:]


if __name__ == '__main__':
    print(json.dumps(monitor(json.loads(sys.argv[1]))))
