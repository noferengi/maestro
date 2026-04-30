import re

path = r'D:\workspace\TheMaestro\app\agent\loop.py'
with open(path, 'r') as f:
    content = f.read()

# Insert the terminal signal check after "self._messages.extend(tool_result_messages)"
# and before "# Check for timeouts in tool results"
old = '''                self._messages.extend(tool_result_messages)

                # Check for timeouts'''
new = '''                self._messages.extend(tool_result_messages)

                # Check for terminal signal from submit_work tool call
                if self._terminal_signal is not None:
                    sig = self._terminal_signal.get("signal")
                    if sig in (SIGNAL_ACCEPTED, SIGNAL_REVERT):
                        return self._handle_terminal(self._terminal_signal)

                # Check for timeouts'''

if old in content:
    content = content.replace(old, new, 1)
    with open(path, 'w') as f:
        f.write(content)
    print("SUCCESS: inserted terminal signal check")
else:
    print("ERROR: could not find target string")
    idx = content.find('self._messages.extend(tool_result_messages)')
    if idx >= 0:
        chunk = content[idx:idx+200]
        print(f"Found at {idx}: {repr(chunk)}")
