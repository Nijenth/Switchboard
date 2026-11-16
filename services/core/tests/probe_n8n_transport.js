// Run inside the pinned n8n image. A new process creates a new agent per sample.
const fs = require("node:fs");
const http = require("node:http");
const { performance } = require("node:perf_hooks");

const [base, sample, batches, concurrency, reuse] = process.argv.slice(2);
const agent = new http.Agent({ keepAlive: reuse === "reuse" });

function kernelTcpState(socket) {
  // On Linux, inspect only the timing probe socket. 02 is TCP SYN_SENT.
  try {
    const target = fs.readlinkSync("/proc/self/fd/" + socket._handle.fd);
    const inode = /^socket:\[(\d+)\]$/.exec(target)[1];
    for (const table of ["/proc/net/tcp", "/proc/net/tcp6"]) {
      for (const line of fs.readFileSync(table, "utf8").split("\n")) {
        const fields = line.trim().split(/\s+/);
        if (fields[9] === inode) return fields[3];
      }
    }
  } catch {
    return undefined;
  }
}

function request(id) {
  return new Promise((resolve) => {
    const started = performance.now();
    const entry = { event: "client", id, start_ms: Date.now() };
    const elapsed = () => Math.round((performance.now() - started) * 1000) / 1000;
    let settled = false;
    const req = http.get(base + "/" + id, { agent }, (res) => {
      entry.headers_ms = elapsed();
      entry.status = res.statusCode;
      res.resume();
      res.on("end", () => finish());
      res.on("error", (error) => finish(error.code ?? error.message));
    });
    function finish(error) {
      if (settled) return;
      settled = true;
      clearTimeout(deadline);
      if (error) entry.error = error;
      entry.done_ms = elapsed();
      console.log(JSON.stringify(entry));
      resolve();
    }
    // The deadline also covers lookup and connect. No requests are retried.
    const deadline = setTimeout(() => {
      if (req.socket?.connecting) entry.kernel_tcp_state = kernelTcpState(req.socket);
      req.destroy(new Error("deadline"));
    }, 5000);
    req.on("socket", (socket) => {
      entry.socket_ms = elapsed();
      entry.reused = req.reusedSocket;
      if (!socket.connecting) entry.port = socket.localPort;
      else {
        socket.once("lookup", (error, address) => {
          entry.lookup_ms = elapsed();
          entry.address = address;
        });
        socket.once("connect", () => {
          entry.connect_ms = elapsed();
          entry.port = socket.localPort;
        });
      }
    });
    req.on("finish", () => { entry.sent_ms = elapsed(); });
    req.on("error", (error) => finish(error.code ?? error.message));
  });
}

(async () => {
  for (let batch = 0; batch < Number(batches); batch++) {
    const width = batch % 2 === 0 ? 2 : Number(concurrency);
    await Promise.all(Array.from({ length: width }, (_, i) => request(sample + "-" + batch + "-" + i)));
  }
  agent.destroy();
})();
