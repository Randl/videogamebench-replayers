// Human-recorder extension. Inputs continue through the upstream CommandInterface.
(() => {
    const OriginalWorker = window.Worker;
    const pending = new Map(), workers = new Map();
    let sequence = 0, ci = null;
    window.Worker = class extends OriginalWorker {
        constructor(...args) {
            super(...args);
            this.addEventListener('message', event => {
                const reply = event.data;
                if (!reply?.vgbState) return;
                const handler = pending.get(reply.vgbState);
                if (!handler) return;
                pending.delete(reply.vgbState);
                clearTimeout(handler.timer);
                reply.error ? handler.reject(new Error(reply.error)) : handler.resolve(reply.result);
            });
        }
        postMessage(data, ...args) {
            if (data?.props?.sessionId) workers.set(data.props.sessionId, this);
            super.postMessage(data, ...args);
        }
    };
    async function command(operation, slot, bytes) {
        if (!ci || ci.transport.net) throw new Error('Local DOSBox state interface is not ready');
        const worker = workers.get(ci.transport.sessionId);
        if (!worker) throw new Error('DOSBox worker was not found');
        const request = String(++sequence);
        return new Promise((resolve, reject) => {
            const timer = setTimeout(() => {
                pending.delete(request);
                reject(new Error('DOSBox save-state operation timed out'));
            }, VGB_STATE_CONFIG.timeout_ms);
            pending.set(request, {resolve, reject, timer});
            worker.postMessage({name: 'wc-vgb-state', props: {
                sessionId: ci.transport.sessionId, operation, slot, request, bytes,
            }}, bytes ? [bytes.buffer] : []);
        });
    }
    async function readBounded(stream) {
        const reader = stream.getReader(), chunks = [];
        let length = 0;
        for (;;) {
            const {value, done} = await reader.read();
            if (done) break;
            length += value.length;
            if (length > VGB_STATE_CONFIG.max_bytes) {
                await reader.cancel(); throw new Error('Snapshot exceeds configured size limit');
            }
            chunks.push(value);
        }
        const bytes = new Uint8Array(length);
        let offset = 0;
        for (const chunk of chunks) {bytes.set(chunk, offset); offset += chunk.length;}
        return bytes;
    }
    window.vgbStates = {
        bind(value) {ci = value;},
        front(hostMs = Date.now()) {return {elapsed_ms: hostMs - ci.startedAt, keys: {...ci.keyMatrix}};},
        setFront(state, hostMs = Date.now()) {ci.startedAt = hostMs - state.elapsed_ms; ci.keyMatrix = {...state.keys};},
        shiftClock(ms) {ci.startedAt += ms;},
        command,
        async upload({slot, url, operation = 'export'}) {
            const {bytes} = await command(operation, slot);
            const compressed = await readBounded(new Blob([bytes]).stream().pipeThrough(new CompressionStream('gzip')));
            for (let offset = 0; offset < compressed.length; offset += VGB_STATE_CONFIG.chunk_bytes) {
                const response = await fetch(url + '/' + offset, {method: 'POST',
                    body: compressed.subarray(offset, offset + VGB_STATE_CONFIG.chunk_bytes)});
                if (!response.ok) throw new Error(await response.text());
            }
            return {bytes: compressed.length, uncompressed_bytes: bytes.length};
        },
        async download({slot, url}) {
            const response = await fetch(url);
            if (!response.ok) throw new Error(await response.text());
            const bytes = await readBounded(response.body.pipeThrough(new DecompressionStream('gzip')));
            return await command('import', slot, bytes);
        },
    };
})();
