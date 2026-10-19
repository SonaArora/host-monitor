"""
Used to read and print the processed metrics from Redis streams for debugging purposes.
"""

import redis
import json

r = redis.Redis(host="localhost", port=6379, decode_responses=True)

streams = r.keys("processed_metrics_*")

# streams = ["processed_metrics_fedora"]
#   msg_id = "1792287370068-0"
#   fields = {"metrics": '{"hostname": "fedora", "timestamp": "...",
#             "metrics": {"cpu_percent": 6.45, "memory_percent": 31.9, ...}
#               ...}

if not streams:
    print("No metric streams found in Redis")
else:
    # start from the latest message for each stream
    stream_offsets = {stream: "$" for stream in streams}
    print(stream_offsets)
    print(f"Watching streams: {streams}. Press Ctrl+C to stop.\n")
    try:
        while True:
            results = r.xread(stream_offsets, block=0)
            print(f"Results: {results}")
            for stream, messages in results:
                print(f"stream: {stream}")
                print(f"messages: {messages}")
                for msg_id, fields in messages:
                    data = json.loads(fields["metrics"])
                    hostname = data["hostname"]
                    print(f"\n=== Host: {hostname} | ID: {msg_id} ===")
                    print(json.dumps(data, indent=2))
                    stream_offsets[stream] = msg_id
    except KeyboardInterrupt:
        print("\nStopped.")
