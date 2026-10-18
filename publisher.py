import os

import redis
import json


class RedisPublisher:

    envhost = os.environ.get("REDIS_HOST", "localhost")
    envport = os.environ.get("REDIS_PORT", 6379)

    def __init__(self, host=envhost, port=envport, max_stream_length=1000):

        self.client = redis.Redis(host=host, port=port)
        self.max_stream_length = max_stream_length

    def publish(self, stream, metrics):
        payload = {"metrics": json.dumps(metrics)}
        self.client.xadd(stream, payload, maxlen=self.max_stream_length)
        print(f"Published to stream '{stream}'")

    def close(self):
        self.client.close()
