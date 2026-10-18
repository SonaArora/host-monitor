import os

import psutil
from datetime import datetime, timezone
import time
import json
import platform
from abc import ABC, abstractmethod
from publisher import RedisPublisher

try:
    import pynvml

    pynvml.nvmlInit()
    NVML_AVAILABLE = True
except Exception:
    NVML_AVAILABLE = False


class Collector(ABC):
    def __init__(self):
        self.hostname = platform.node()
        self.metrics = {}

    @abstractmethod
    def collect(self):
        pass

    def report(self):
        try:
            self.metrics = self.collect()
            # print(json.dumps(self.metrics, indent=2))
        except Exception as e:
            print(f"Error collecting {self.name}: {e}")


class CPUMetrics(Collector):
    def __init__(self, interval):
        super().__init__()
        self.interval = interval

    def collect(self):
        cpu_metrics = {}
        # print("Timestamp:", datetime.now(timezone.utc).isoformat())
        # print(f"psutil.cpu_times():", psutil.cpu_times())
        cpu_usage = psutil.cpu_times()
        # print(psutil.cpu_times_percent())
        # total_cpu_usage = round(100 - cpu_usage.idle - cpu_usage.iowait, 2)

        try:
            loadavg_1m, loadavg_5m, loadavg_15m = psutil.getloadavg()
        except (AttributeError, OSError):
            loadavg_1m, loadavg_5m, loadavg_15m = None

        # cpu_metrics["physical_cores"] = psutil.cpu_count(logical=False)
        cpu_metrics["logical_cpus"] = psutil.cpu_count(logical=True)
        # cpu_metrics["total_cpu_usage"] = total_cpu_usage
        cpu_metrics["cpu_usage_user"] = cpu_usage.user
        cpu_metrics["cpu_usage_nice"] = cpu_usage.nice
        cpu_metrics["cpu_usage_system"] = cpu_usage.system
        cpu_metrics["cpu_usage_idle"] = cpu_usage.idle
        cpu_metrics["cpu_usage_iowait"] = cpu_usage.iowait
        cpu_metrics["cpu_usage_irq"] = cpu_usage.irq
        cpu_metrics["cpu_usage_softirq"] = cpu_usage.softirq
        cpu_metrics["cpu_usage_steal"] = cpu_usage.steal
        cpu_metrics["cpu_usage_guest"] = cpu_usage.guest
        cpu_metrics["loadavg_1m"] = round(loadavg_1m, 2)
        cpu_metrics["loadavg_5m"] = round(loadavg_5m, 2)
        cpu_metrics["loadavg_15m"] = round(loadavg_15m, 2)

        return cpu_metrics


class MemoryMetrics(Collector):
    def __init__(self, interval):
        super().__init__()
        self.interval = interval

    def collect(self):

        memory_metrics = {}
        # print("Timestamp:", datetime.now(timezone.utc).isoformat())
        memory_in_bytes = psutil.virtual_memory()
        swap_memory_in_bytes = psutil.swap_memory()

        unit_mb = 2**20

        memory_metrics["total_memory"] = round(memory_in_bytes.total / unit_mb, 2)
        memory_metrics["available_memory"] = round(
            memory_in_bytes.available / unit_mb, 2
        )
        memory_metrics["used_memory"] = round(memory_in_bytes.used / unit_mb, 2)
        memory_metrics["free_memory"] = round(memory_in_bytes.free / unit_mb, 2)
        memory_metrics["used_percent"] = round(memory_in_bytes.percent)
        memory_metrics["buffer_memory"] = round(memory_in_bytes.buffers / unit_mb, 2)
        memory_metrics["cache_memory"] = round(memory_in_bytes.cached / unit_mb, 2)
        memory_metrics["shared_memory"] = round(memory_in_bytes.shared / unit_mb, 2)
        memory_metrics["active_memory"] = round(memory_in_bytes.active / unit_mb, 2)
        memory_metrics["slab_memory"] = round(memory_in_bytes.slab / unit_mb, 2)
        memory_metrics["total_swap"] = round(swap_memory_in_bytes.total / unit_mb, 2)
        memory_metrics["used_swap"] = round(swap_memory_in_bytes.used / unit_mb, 2)
        memory_metrics["free_swap"] = round(swap_memory_in_bytes.free / unit_mb, 2)
        memory_metrics["swap_in"] = round(swap_memory_in_bytes.sin / unit_mb, 2)
        memory_metrics["swap_out"] = round(swap_memory_in_bytes.sout / unit_mb, 2)

        return memory_metrics


class DiskMetrics(Collector):
    def __init__(self, interval):
        super().__init__()
        self.interval = interval

    def collect(self):
        disk_info = []
        unit_gb = 2**30
        io_info = {}

        # print("Timestamp:", datetime.now(timezone.utc).isoformat())

        for partition in psutil.disk_partitions():
            try:
                print(time.time())
                usage = psutil.disk_usage(partition.mountpoint)
                disk_info.append(
                    {
                        "device": partition.device,
                        "mountpoint": partition.mountpoint,
                        "fstype": partition.fstype,
                        "total_size_gb": round(usage.total / unit_gb, 2),
                        "used_size_gb": round(usage.used / unit_gb, 2),
                        "free_size_gb": round(usage.free / unit_gb, 2),
                        "usage_percent": round(usage.percent, 2),
                    }
                )
            except (OSError, PermissionError):
                continue

            # print(f"io count",time.time())

            io = psutil.disk_io_counters(perdisk=True)

            for disk, io_values in io.items():
                io_info.update(
                    {
                        disk: {
                            "read_count": io_values.read_count,
                            "write_count": io_values.write_count,
                            "read_bytes": io_values.read_bytes,
                            "read_time": io_values.read_time,
                            "write_time": io_values.write_time,
                            "busy_time": io_values.busy_time,
                        }
                    }
                )

        return disk_info, io_info


class GPUMetrics(Collector):
    def __init__(self, interval):
        super().__init__()
        self.interval = interval
        if not NVML_AVAILABLE:
            raise RuntimeError("No NVIDIA GPU or driver found.")
        self.device_count = pynvml.nvmlDeviceGetCount()

    def collect(self):
        print("Timestamp:", datetime.now(timezone.utc).isoformat())
        unit_mb = 2**20
        gpu_metrics = []

        for i in range(self.device_count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
            util = pynvml.nvmlDeviceGetUtilizationRates(handle)

            try:
                power_draw = pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0
                power_limit = pynvml.nvmlDeviceGetEnforcedPowerLimit(handle) / 1000.0
            except pynvml.NVMLError:
                power_draw = power_limit = None

            try:
                fan_speed = pynvml.nvmlDeviceGetFanSpeed(handle)
            except pynvml.NVMLError:
                fan_speed = None

            try:
                temp = pynvml.nvmlDeviceGetTemperature(
                    handle, pynvml.NVML_TEMPERATURE_GPU
                )
            except pynvml.NVMLError:
                temp = None

            try:
                clock_graphics = pynvml.nvmlDeviceGetClockInfo(
                    handle, pynvml.NVML_CLOCK_GRAPHICS
                )
                clock_mem = pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_MEM)
                clock_sm = pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_SM)
            except pynvml.NVMLError:
                clock_graphics = clock_mem = clock_sm = None

            gpu_metrics.append(
                {
                    "index": i,
                    "name": pynvml.nvmlDeviceGetName(handle),
                    "gpu_utilization_percent": util.gpu,
                    "memory_utilization_percent": util.memory,
                    "memory_total_mb": round(mem.total / unit_mb, 2),
                    "memory_used_mb": round(mem.used / unit_mb, 2),
                    "memory_free_mb": round(mem.free / unit_mb, 2),
                    "temperature_c": temp,
                    "power_draw_w": power_draw,
                    "power_limit_w": power_limit,
                    "fan_speed_percent": fan_speed,
                    "clock_graphics_mhz": clock_graphics,
                    "clock_memory_mhz": clock_mem,
                    "clock_sm_mhz": clock_sm,
                }
            )

        return gpu_metrics

    def __del__(self):
        if NVML_AVAILABLE:
            try:
                pynvml.nvmlShutdown()
            except Exception:
                pass


def main():
    publisher = RedisPublisher()

    collectors = [CPUMetrics(1), MemoryMetrics(1), DiskMetrics(1)]
    if NVML_AVAILABLE:
        collectors.append(GPUMetrics(1))

    hostname = collectors[0].hostname
    stream = f"metrics.{hostname}"

    scrape_interval = int(os.environ.get("SCRAPE_INTERVAL", 5))
    try:
        while True:
            for collector in collectors:
                collector.report()
            combined_metrics = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "cpu": collectors[0].metrics,
                "memory": collectors[1].metrics,
                "disk": collectors[2].metrics,
            }
            if NVML_AVAILABLE:
                combined_metrics["gpu"] = collectors[3].metrics

            publisher.publish(stream, combined_metrics)
            time.sleep(scrape_interval)
    except KeyboardInterrupt:
        print("\nStopping collector...")
    finally:
        publisher.close()


if __name__ == "__main__":
    main()
