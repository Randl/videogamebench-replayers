// Optional execution trace: WASM and game assets remain byte-for-byte upstream.
let vgbReplay = VGB_EXECUTION_CONFIG.replay !== null;
let vgbEntries = vgbReplay ? VGB_EXECUTION_CONFIG.replay.events : [];
let vgbIndex = 0, vgbDepth = 0, vgbDriving = false, vgbStopped = false, vgbError = null;
let vgbClockEntry = null, vgbClockRemaining = 0, vgbHead = null, vgbPaused = false;
let vgbChunkIndex = 0, vgbLastMarker = null, vgbHold = false;
const vgbReplayClock = {};
const vgbRealDate = Date.now.bind(Date), vgbRealNow = performance.now.bind(performance);
const vgbInputNames = new Set(['wc-add-key', 'wc-mouse-move', 'wc-mouse-button', 'wc-mouse-sync',
    'wc-asyncify-stats', 'wc-mute', 'wc-unmute', 'wc-pause', 'wc-resume', 'wc-get-running-program']);
function vgbFail(error) {vgbError = String(error); vgbStopped = true; console.error(vgbError); throw error;}
function vgbAppend(entry) {
    if (vgbEntries.length >= VGB_EXECUTION_CONFIG.max_pending_events) vgbFail(Error('Execution buffer full; recording stopped without dropping events'));
    vgbEntries.push(entry);
}
function vgbClock(kind, real) {
    if (vgbPaused) return real();
    if (!vgbReplay) {
        const value = real(), last = vgbEntries.at(-1);
        if (last && last[0] === kind && last[1] === value && last[2] < 4294967295) ++last[2];
        else vgbAppend([kind, value, 1]);
        return value;
    }
    if (!vgbClockRemaining) {vgbClockEntry = vgbEntries[vgbIndex++]; vgbClockRemaining = vgbClockEntry?.[2] || 1;}
    const entry = vgbClockEntry; --vgbClockRemaining;
    if (!entry || entry[0] !== kind) return vgbFail(Error('Clock mismatch at chunk ' + vgbChunkIndex + ' event ' + (vgbIndex - 1)));
    vgbReplayClock[kind === 1 ? 'date' : 'performance'] = entry[1];
    return entry[1];
}
function vgbDateNow() {return vgbClock(1, () => Module.vgbClockSources ? Module.vgbClockSources.date() : vgbRealDate());}
function vgbPerformanceNow() {return vgbClock(0, () => Module.vgbClockSources ? Module.vgbClockSources.performance() : vgbRealNow());}
function vgbNativeCall(fn) {++vgbDepth; try {return fn();} catch(e) {return vgbFail(e);} finally {--vgbDepth;}}
function vgbWrapWake(wake) {
    return (...args) => {
        if (vgbStopped || vgbPaused) return;
        if (!vgbReplay) vgbAppend([2]);
        return vgbNativeCall(() => wake(...args));
    };
}
async function vgbHash(bytes) {
    return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), x => x.toString(16).padStart(2, '0')).join('');
}
async function vgbFingerprint() {
    const heap = HEAPU8.slice();
    const [pointer, width, height] = Module.snapshotFrame;
    return {...(Module.vgbFingerprintExtra ? await Module.vgbFingerprintExtra(heap) : {}), heap_sha256: await vgbHash(heap), frame_sha256: await vgbHash(heap.subarray(pointer, pointer + width * height * 3)), width, height};
}
async function vgbDrive() {
    if (!vgbReplay || vgbStopped || vgbHold) return;
    const deadline = vgbRealNow() + VGB_EXECUTION_CONFIG.slice_ms;
    try {
        while (!vgbStopped && !vgbHold && vgbRealNow() < deadline) {
            if (vgbIndex === vgbEntries.length) {
                if (vgbClockRemaining) throw Error('Unconsumed clock readings at chunk boundary');
                const chunks = VGB_EXECUTION_CONFIG.replay.chunks;
                if (++vgbChunkIndex >= chunks.length) {
                    if (!VGB_EXECUTION_CONFIG.capture_prefix) throw Error('Trace ended without final verification');
                    // Reconstruction only, not verification. The exported state must
                    // subsequently pass an independent replay with a final fingerprint.
                    vgbLastMarker = 'prefix'; vgbStopped = true; vgbPaused = true;
                    vgbHead = chunks[chunks.length - 1];
                    break;
                }
                vgbEntries = await Module.vgbLoadChunk(chunks[vgbChunkIndex]); vgbIndex = 0;
            }
            const entry = vgbEntries[vgbIndex++];
            if (entry[0] === 2) {
                const wake = Module.sync_wakeUp;
                if (!wake) throw Error('No suspended continuation');
                delete Module.sync_wakeUp; delete Module.wakeUpAt; wake();
            } else if (entry[0] === 3) {
                vgbDriving = true;
                try {Module.vgbProcess({name: entry[1], props: entry[2]});} finally {vgbDriving = false;}
            } else if (entry[0] === 4) {
                const actual = await vgbFingerprint();
                if (Object.keys(entry[2]).some(key=>actual[key]!==entry[2][key])) throw Error('Execution state mismatch at ' + entry[1] + ': expected ' + JSON.stringify(entry[2]) + ', actual ' + JSON.stringify(actual));
                vgbLastMarker = entry[1];
                if (entry[1] === 'end') vgbStopped = true;
                else if (VGB_EXECUTION_CONFIG.hold_markers) vgbHold = true;
            } else throw Error('Unexpected event outside native execution: ' + entry[0]);
        }
    } catch (e) {vgbError = String(e); vgbStopped = true; console.error(vgbError);}
    if (!vgbStopped && !vgbHold) setTimeout(vgbDrive, 0);
}
Module.vgbExecution = {
    get replay() {return vgbReplay;},
    get blocked() {return vgbStopped || vgbPaused;},
    snapshotClock() {return vgbReplay && vgbStopped && vgbLastMarker === 'prefix' ? vgbReplayClock : null;},
    pause() {vgbPaused = true;},
    resume() {vgbPaused = false; if (!vgbReplay && !vgbStopped) setTimeout(() => Module.receive({data:{name:'wc-sync-sleep',props:{sessionId:Module.sessionId}}}), 0);},
    capture() {if (vgbReplay && vgbStopped && vgbLastMarker === 'prefix') return {head:vgbHead};if (vgbEntries.length) throw Error('Execution prefix must be flushed before saving');return {head:vgbHead};},
    restore(cursor) {if (!cursor?.head || vgbReplay) throw Error('This save has no reproducible execution prefix; start a new recording');vgbEntries=[];vgbHead=cursor.head;},
    async command(operation, value) {
        if (operation === 'exec-heap') return {bytes:HEAPU8.slice()};
        if (operation === 'exec-fingerprint') return await vgbFingerprint();
        if (operation === 'exec-replay-suffix') {
            // Reconstruction from a verified snapshot. Never switch a live,
            // unflushed recording or attach a suffix to a different prefix.
            if (vgbError || vgbReplay || !vgbPaused || vgbEntries.length || value.parent !== vgbHead || !Array.isArray(value.chunks) || !value.chunks.length) throw Error('Suffix replay requires a paused, flushed matching snapshot');
            VGB_EXECUTION_CONFIG.replay = {chunks:value.chunks, base_url:value.base_url};
            const events = await Module.vgbLoadChunk(value.chunks[0], vgbHead);
            vgbEntries = events; vgbIndex = 0; vgbChunkIndex = 0;
            vgbClockEntry = null; vgbClockRemaining = 0;
            vgbLastMarker = null; vgbStopped = false; vgbHold = true;
            VGB_EXECUTION_CONFIG.capture_prefix = true; vgbReplay = true;
            return {head:vgbHead};
        }
        if (operation === 'exec-status') return {stopped:vgbStopped,error:vgbError,marker:vgbLastMarker,head:vgbHead,chunk:vgbChunkIndex,...(vgbLastMarker === 'prefix' ? {clock:vgbReplayClock} : {})};
        if (vgbError) throw Error(vgbError);
        if (operation === 'exec-advance') {vgbHold=false;vgbDrive();return {};}
        if (vgbReplay) throw Error('Recording operation during replay');
        if (operation === 'exec-drain') {const events=vgbEntries;vgbEntries=[];return {parent:vgbHead,events};}
        if (operation === 'exec-head') {vgbHead=value;return {};}
        if (operation === 'exec-marker' || operation === 'exec-stop') {
            if (vgbStopped) return {marker:vgbLastMarker};
            vgbPaused=true;
            const fingerprint=await vgbFingerprint();
            const name=operation==='exec-stop'?'end':value;
            vgbAppend([4,name,fingerprint]);vgbLastMarker=name;
            if(operation==='exec-stop')vgbStopped=true;
            this.resume();return {marker:name,...fingerprint};
        }
        throw Error('Unknown execution operation');
    },
};
Module.vgbArgs = args => {
    Module.vgbLiveSessionId=args[0];
    if (vgbReplay) {const e=vgbEntries[vgbIndex++];if(e?.[0]!==5)throw Error('Missing boot arguments');return e[1].slice();}
    vgbAppend([5,args.slice()]);return args;
};
