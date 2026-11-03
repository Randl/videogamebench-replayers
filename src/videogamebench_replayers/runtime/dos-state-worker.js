// Runs inside the checksum-pinned WDOSBOX worker closure. No emulator instructions change.
const stateBuild = VGB_STATE_CONFIG.wasm_sha256;
const snapshotModuleKeys = ['snapshotFrame', 'snapshotAudioRate', 'fsCreatedAt', 'sleep_started_at',
    'last_wakeup', 'wakeUpAt', 'sleep_count', 'sleep_time', 'nonskippable_sleep_count'];
function checkSnapshotSize(bytes) {
    if (bytes > VGB_STATE_CONFIG.max_bytes) throw new Error(`Snapshot requires at least ${bytes} bytes; configured limit is ${VGB_STATE_CONFIG.max_bytes}. Increase --dos-state-max-bytes.`);
}
function capturePortableFs() {
    const entries = [];
    let totalBytes = HEAPU8.length;
    function visit(path) {
        if (path === '/dev' || path === '/proc') return;
        const node = FS.lookupPath(path, {follow: false}).node;
        const entry = {path, mode: node.mode, id: node.id, atime: node.atime, mtime: node.mtime, ctime: node.ctime};
        if (FS.isDir(node.mode)) {
            entries.push(entry);
            for (const name of FS.readdir(path).filter(n => n !== '.' && n !== '..')) {
                visit((path === '/' ? '' : path) + '/' + name);
            }
        } else if (FS.isFile(node.mode)) {
            totalBytes += node.usedBytes || 0;
            checkSnapshotSize(totalBytes);
            entry.contents = node.contents ? node.contents.slice(0, node.usedBytes) : new Uint8Array();
            entries.push(entry);
        } else if (FS.isLink(node.mode)) {
            entry.link = node.link;
            entries.push(entry);
        } else throw new Error('Unsupported filesystem node: ' + path);
    }
    visit('/');
    const shared = new Map();
    const streams = FS.streams.filter(Boolean).map(stream => {
        const path = FS.getPath(stream.node);
        if (stream.fd > 2 && !FS.isFile(stream.node.mode) && !FS.isDir(stream.node.mode)) {
            throw new Error('Unsupported open stream: ' + path);
        }
        if (stream.fd > 2 && FS.lookupPath(path).node !== stream.node) {
            throw new Error('Unlinked open files are not supported');
        }
        if (!shared.has(stream.shared)) shared.set(stream.shared, shared.size);
        return {fd: stream.fd, path, flags: stream.flags, position: stream.position,
            shared: shared.get(stream.shared), seekable: stream.seekable,
            getdents: stream.getdents ? [...stream.getdents] : stream.getdents};
    });
    return {entries, streams, cwd: FS.cwd(), nextInode: FS.nextInode,
        ttys: TTY.ttys.map(tty => tty && ({input: [...tty.input], output: [...tty.output]}))};
}

function restorePortableFs(state) {
    for (const stream of FS.streams.slice()) if (stream && stream.fd > 2) FS.close(stream);
    function remove(path) {
        const node = FS.lookupPath(path, {follow: false}).node;
        if (FS.isDir(node.mode)) {
            for (const name of FS.readdir(path).filter(n => n !== '.' && n !== '..')) remove(path + '/' + name);
            FS.rmdir(path);
        } else FS.unlink(path);
    }
    FS.chdir('/');
    for (const name of FS.readdir('/')) if (!['.', '..', 'dev', 'proc'].includes(name)) remove('/' + name);
    for (const entry of state.entries) {
        if (entry.path !== '/') {
            if (FS.isDir(entry.mode)) FS.mkdir(entry.path, entry.mode);
            else if (FS.isLink(entry.mode)) FS.symlink(entry.link, entry.path);
            else FS.writeFile(entry.path, entry.contents, {mode: entry.mode});
        }
        const node = FS.lookupPath(entry.path, {follow: false}).node;
        FS.hashRemoveNode(node);
        Object.assign(node, {id: entry.id, mode: entry.mode, atime: entry.atime, mtime: entry.mtime, ctime: entry.ctime});
        FS.hashAddNode(node);
    }
    // Child creation updates parent timestamps. Reapply them after building the
    // tree; inode IDs above must be set before children enter the hash table.
    for (const entry of state.entries) {
        Object.assign(FS.lookupPath(entry.path, {follow:false}).node,
            {atime:entry.atime,mtime:entry.mtime,ctime:entry.ctime});
    }
    const shared = new Map();
    for (const saved of state.streams) {
        let stream;
        if (saved.fd <= 2) stream = FS.streams[saved.fd];
        else {
            stream = FS.open(saved.path, saved.flags & ~(64 | 128 | 512));
            delete FS.streams[stream.fd];
            stream.fd = saved.fd;
            FS.streams[saved.fd] = stream;
        }
        if (!shared.has(saved.shared)) shared.set(saved.shared, {flags: saved.flags, position: saved.position});
        stream.shared = shared.get(saved.shared);
        stream.seekable = saved.seekable;
        stream.getdents = saved.getdents ? [...saved.getdents] : saved.getdents;
    }
    FS.nextInode = state.nextInode;
    FS.chdir(state.cwd);
    state.ttys.forEach((tty, index) => {if (tty) Object.assign(TTY.ttys[index], {input: [...tty.input], output: [...tty.output]});});
}

function exportAsyncify(state) {
    const exports = Object.entries(wasmExports);
    const functions = [];
    for (const [id, fn] of state.callStackIdToFunc) {
        const exported = exports.find(([, value]) => value === fn || value === Asyncify.funcWrappers.get(fn));
        if (!exported) throw new Error('Unidentified Asyncify entry point');
        functions.push([id, exported[0]]);
    }
    return {currData: state.currData, handleSleepReturnValue: state.handleSleepReturnValue,
        callStackId: state.callStackId, functions};
}

function importAsyncify(state) {
    const callStackIdToFunc = new Map();
    const callstackFuncToId = new Map();
    for (const [id, name] of state.functions) {
        const wrapper = wasmExports[name];
        const original = [...Asyncify.funcWrappers].find(([, fn]) => fn === wrapper)?.[0];
        if (!original) throw new Error('Incompatible Asyncify entry point: ' + name);
        callStackIdToFunc.set(id, original);
        callstackFuncToId.set(original, id);
    }
    return {...state, callStackIdToFunc, callstackFuncToId};
}

function encodePortable(state) {
    const chunks = [state.heap];
    let offset = state.heap.length;
    const files = state.portableFs.entries.map(entry => {
        if (!entry.contents) return entry;
        const record = {...entry, contents: [offset, entry.contents.length]};
        chunks.push(entry.contents); offset += entry.contents.length;
        return record;
    });
    const module = {...state.module}; delete module.bundles; delete module.files;
    const metadata = {version: 1, build: stateBuild, identity: VGB_STATE_CONFIG.identity, heapSize: state.heap.length,
        fs: {...state.portableFs, entries: files}, module,
        execution: state.execution, asyncify: state.asyncify, keepalive: state.keepalive,
        date: state.date, performance: state.performance};
    const header = new TextEncoder().encode(JSON.stringify(metadata));
    checkSnapshotSize(12 + header.length + offset);
    const bytes = new Uint8Array(12 + header.length + offset);
    bytes.set(new TextEncoder().encode('VGBDOS01'));
    new DataView(bytes.buffer).setUint32(8, header.length, true);
    bytes.set(header, 12); offset = 12 + header.length;
    for (const chunk of chunks) {bytes.set(chunk, offset); offset += chunk.length;}
    return bytes;
}

function decodePortable(bytes) {
    if (!(bytes instanceof Uint8Array) || bytes.length > VGB_STATE_CONFIG.max_bytes || bytes.length < 12 || new TextDecoder().decode(bytes.subarray(0, 8)) !== 'VGBDOS01') {
        throw new Error('Invalid DOSBox state');
    }
    const length = new DataView(bytes.buffer, bytes.byteOffset).getUint32(8, true);
    if (length > 2 * 1024 * 1024 || length + 12 >= bytes.length) throw new Error('Invalid state header');
    const metadata = JSON.parse(new TextDecoder().decode(bytes.subarray(12, length + 12)));
    if (metadata.version !== 1 || metadata.build !== stateBuild || metadata.identity !== VGB_STATE_CONFIG.identity) throw new Error('Incompatible DOSBox state build');
    const data = bytes.subarray(12 + length);
    if (!Number.isSafeInteger(metadata.heapSize) || metadata.heapSize < 65536 || metadata.heapSize % 65536 || metadata.heapSize > data.length) throw new Error('Invalid heap size');
    const entries = metadata.fs.entries.map(entry => {
        if (entry.path !== '/' && (!entry.path.startsWith('/') || entry.path.split('/').some(p => p === '.' || p === '..') || /^\/(dev|proc)(\/|$)/.test(entry.path))) {
            throw new Error('Invalid filesystem path');
        }
        if (!entry.contents) return entry;
        const [offset, length] = entry.contents;
        if (!Number.isSafeInteger(offset) || !Number.isSafeInteger(length) || offset < metadata.heapSize || length < 0 || offset + length > data.length) throw new Error('Invalid file data');
        return {...entry, contents: data.slice(offset, offset + length)};
    });
    return {heap: data.slice(0, metadata.heapSize), portableFs: {...metadata.fs, entries},
        execution: metadata.execution, module: metadata.module, asyncify: metadata.asyncify, keepalive: metadata.keepalive,
        date: metadata.date, performance: metadata.performance};
}

const stateCache = new Map();
const realDateNow = Date.now.bind(Date);
const realPerformanceNow = performance.now.bind(performance);
let dateOffset = 0, performanceOffset = 0, frozenClock = null, stateBusy = false;
Date.now = () => frozenClock ? frozenClock.date : realDateNow() - dateOffset;
const snapshotPerformanceNow = () => frozenClock ? frozenClock.performance : realPerformanceNow() - performanceOffset;
if (Module.vgbExecution) Module.vgbClockSources = {date: () => Date.now(), performance: snapshotPerformanceNow};
else _emscripten_get_now = snapshotPerformanceNow;

function cacheState(slot, state) {
    stateCache.delete(slot);
    stateCache.set(slot, state);
    while (stateCache.size > VGB_STATE_CONFIG.cache_size) stateCache.delete(stateCache.keys().next().value);
}

async function freezeState() {
    if (frozenClock) return;
    if (!Module.alive || Object.keys(Module.sockdrives).length || Module.glfx) {
        throw new Error('Save states require a running regular DOSBox with local files and no 3Dfx');
    }
    if (Module.vgbExecution) Module.vgbExecution.pause();
    else Module._requestPause();
    // No emulated execution can run concurrently with this worker callback.
    // Freeze clocks at the pause request, not after the settling/copying delay.
    const replayClock = Module.vgbExecution?.snapshotClock?.();
    frozenClock = {date: replayClock?.date ?? Date.now(), performance: replayClock?.performance ?? snapshotPerformanceNow(),
        frozenAt: realPerformanceNow(), hostDate: realDateNow()};
    await new Promise(resolve => setTimeout(resolve, VGB_STATE_CONFIG.settle_ms));
    if (Asyncify.state !== 0 || !Asyncify.currData || !Module.sync_wakeUp || Asyncify.exportCallStack.length) {
        resumeState();
        throw new Error('DOSBox did not reach a suspended Asyncify boundary');
    }
}

function resumeState() {
    let pausedMs = 0;
    if (frozenClock) {
        pausedMs = realPerformanceNow() - frozenClock.frozenAt;
        dateOffset = realDateNow() - frozenClock.date;
        performanceOffset = realPerformanceNow() - frozenClock.performance;
        frozenClock = null;
    }
    const resumedHostMs = realDateNow();
    if (Module.vgbExecution) Module.vgbExecution.resume();
    else Module._requestResume();
    return {paused_ms: pausedMs, resumed_host_ms: resumedHostMs};
}

async function stateCommand(command) {
    if (stateBusy) throw new Error('Another save-state operation is running');
    stateBusy = true;
    try {
        if (command.operation === 'heap-size') return {heap_bytes: HEAPU8.length};
        if (command.operation === 'has') return {cached: stateCache.has(command.slot)};
        if (command.operation === 'resume') return resumeState();
        if (command.operation === 'import') {
            const state = decodePortable(command.bytes);
            importAsyncify(state.asyncify);
            cacheState(command.slot, state);
            return {heap_bytes: state.heap.length};
        }
        if (command.operation === 'export') {
            const state = stateCache.get(command.slot);
            if (!state) throw new Error('Snapshot is not cached');
            return {bytes: encodePortable(state)};
        }
        if (!['save', 'restore', 'pause'].includes(command.operation)) throw new Error('Unknown save-state operation');
        if (command.operation === 'restore' && !stateCache.has(command.slot)) throw new Error('Snapshot is not cached');
        await freezeState();
        if (command.operation === 'pause') return {pause_host_ms: frozenClock.hostDate};
        if (command.operation === 'save') {
            checkSnapshotSize(HEAPU8.length);
            const state = {execution: Module.vgbExecution?.capture(), heap: HEAPU8.slice(), portableFs: capturePortableFs(),
                module: Object.fromEntries(snapshotModuleKeys.map(key => [key, Module[key]])),
                asyncify: exportAsyncify(Asyncify), keepalive: runtimeKeepaliveCounter,
                date: frozenClock.date, performance: frozenClock.performance};
            cacheState(command.slot, state);
            return {heap_bytes: state.heap.length};
        }
        const state = stateCache.get(command.slot);
        // WebAssembly memory cannot shrink. Keeping a larger buffer changes
        // subsequent allocation behavior and breaks replay of the retained prefix.
        if (HEAPU8.length > state.heap.length) throw new Error('Restore requires a fresh emulator: current heap is larger than the saved heap');
        const asyncify = importAsyncify(state.asyncify);
        if (HEAPU8.length < state.heap.length) {
            // The normal Emscripten allocator may overallocate. A snapshot needs
            // exactly the original memory size, including during continuation.
            wasmMemory.grow((state.heap.length - HEAPU8.length) / 65536);
            updateMemoryViews();
        }
        if (Module.vgbExecution) Module.vgbExecution.restore(state.execution);
        restorePortableFs(state.portableFs);
        HEAPU8.fill(0);
        HEAPU8.set(state.heap);
        // JSON omits undefined properties. A disk state without a sleep
        // deadline must clear the discarded branch's deadline, just as a
        // cached state does. Otherwise the restored clock can spend minutes
        // catching up while emulated execution remains asleep.
        for (const key of snapshotModuleKeys) {
            if (state.module[key] === undefined) delete Module[key];
            else Module[key] = state.module[key];
        }
        Object.assign(Asyncify, asyncify);
        runtimeKeepaliveCounter = state.keepalive;
        frozenClock.date = state.date;
        frozenClock.performance = state.performance;
        const [pointer, width, height] = Module.snapshotFrame;
        emsc_ws_client_frame_set_size(width, height);
        if (Module.canvas) Module.updateTexture(pointer, width, height);
        else {
            const pixels = HEAPU8.slice(pointer, pointer + width * height * 3);
            Module.sendMessage('ws-update-lines', {width, height, lines: [{start: 0, heapu8: pixels}]}, [pixels.buffer]);
        }
        if (Module.audioPort && Module.snapshotAudioRate) {
            Module.audioPort.postMessage({type: 'init', sourceRate: Module.snapshotAudioRate});
        }
        return {heap_bytes: state.heap.length};
    } finally {
        stateBusy = false;
    }
}
Module.vgbStateCommand = stateCommand;

if (Module.vgbExecution) Module.vgbFingerprintExtra = async heap => {
    const state = {heap, portableFs:capturePortableFs(),
        module:Object.fromEntries(snapshotModuleKeys.map(key=>[key,Module[key]])),
        asyncify:exportAsyncify(Asyncify), keepalive:runtimeKeepaliveCounter,
        date:0, performance:0};
    return {state_sha256:await vgbHash(encodePortable(state))};
};
