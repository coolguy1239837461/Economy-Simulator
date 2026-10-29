(() => {
  "use strict";

  if (typeof window.ayaInstallDiagnostics === "function")
    return;

  const DEFAULT_DURATION_MS = 120000;
  const DEFAULT_INTERVAL_MS = 5000;

  const finite = value => Number.isFinite(value) ? value : null;
  const round = (value, digits = 3) => {
    if (!Number.isFinite(value))
      return null;
    const scale = 10 ** digits;
    return Math.round(value * scale) / scale;
  };
  const errorDetails = error => ({
    name: error?.name || "Error",
    message: error?.message || String(error),
    stack: error?.stack || null
  });
  const percentile = (sortedValues, fraction) => {
    if (!sortedValues.length)
      return null;
    return sortedValues[Math.min(
      sortedValues.length - 1,
      Math.floor((sortedValues.length - 1) * fraction)
    )];
  };
  const elementRect = element => {
    if (!element)
      return null;
    const rect = element.getBoundingClientRect();
    return {
      left: round(rect.left),
      top: round(rect.top),
      width: round(rect.width),
      height: round(rect.height)
    };
  };
  const canvasDetails = canvas => ({
    id: canvas.id || null,
    className: canvas.className || null,
    backingSize: [canvas.width, canvas.height],
    cssRect: elementRect(canvas),
    display: getComputedStyle(canvas).display,
    visibility: getComputedStyle(canvas).visibility
  });
  const framePacingSummary = (durations, elapsedMs) => {
    const sorted = durations.filter(Number.isFinite).sort((a, b) => a - b);
    const sum = sorted.reduce((total, value) => total + value, 0);
    return {
      observedFrames: sorted.length,
      observedSpanMs: round(elapsedMs),
      fps: elapsedMs > 0 ? round(sorted.length * 1000 / elapsedMs) : null,
      averageFrameMs: sorted.length ? round(sum / sorted.length) : null,
      p50FrameMs: round(percentile(sorted, 0.50)),
      p95FrameMs: round(percentile(sorted, 0.95)),
      p99FrameMs: round(percentile(sorted, 0.99)),
      maxFrameMs: sorted.length ? round(sorted[sorted.length - 1]) : null,
      over16_67Ms: sorted.filter(value => value > 16.67).length,
      over33_34Ms: sorted.filter(value => value > 33.34).length,
      over50Ms: sorted.filter(value => value > 50).length,
      over100Ms: sorted.filter(value => value > 100).length
    };
  };

  const readGpu = canvas => {
    if (!canvas)
      return { available: false };
    try {
      const gl = canvas.getContext("webgl2") || canvas.getContext("webgl");
      if (!gl)
        return { available: false };
      const debugInfo = gl.getExtension("WEBGL_debug_renderer_info");
      return {
        available: true,
        context: typeof WebGL2RenderingContext !== "undefined" && gl instanceof WebGL2RenderingContext
          ? "webgl2" : "webgl",
        vendor: gl.getParameter(gl.VENDOR),
        renderer: gl.getParameter(gl.RENDERER),
        unmaskedVendor: debugInfo ? gl.getParameter(debugInfo.UNMASKED_VENDOR_WEBGL) : null,
        unmaskedRenderer: debugInfo ? gl.getParameter(debugInfo.UNMASKED_RENDERER_WEBGL) : null,
        version: gl.getParameter(gl.VERSION),
        shadingLanguageVersion: gl.getParameter(gl.SHADING_LANGUAGE_VERSION),
        maxTextureSize: gl.getParameter(gl.MAX_TEXTURE_SIZE),
        maxRenderbufferSize: gl.getParameter(gl.MAX_RENDERBUFFER_SIZE),
        maxViewportDimensions: Array.from(gl.getParameter(gl.MAX_VIEWPORT_DIMS) || [])
      };
    } catch (error) {
      return { available: false, error: errorDetails(error) };
    }
  };

  const environmentSnapshot = (config, renderCanvas) => {
    const connection = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
    const navigation = performance.getEntriesByType("navigation")[0];
    return {
      capturedAt: new Date().toISOString(),
      target: config.target,
      buildId: config.buildId || null,
      page: `${location.origin}${location.pathname}`,
      userAgent: navigator.userAgent,
      platform: navigator.userAgentData?.platform || navigator.platform || null,
      mobile: navigator.userAgentData?.mobile ?? null,
      languages: [...(navigator.languages || [])],
      hardwareConcurrency: navigator.hardwareConcurrency || null,
      deviceMemoryGiB: finite(Number(navigator.deviceMemory)),
      crossOriginIsolated: window.crossOriginIsolated,
      secureContext: window.isSecureContext,
      screen: {
        width: screen.width,
        height: screen.height,
        availableWidth: screen.availWidth,
        availableHeight: screen.availHeight,
        colorDepth: screen.colorDepth,
        pixelDepth: screen.pixelDepth
      },
      connection: connection ? {
        effectiveType: connection.effectiveType || null,
        downlinkMbps: finite(connection.downlink),
        roundTripTimeMs: finite(connection.rtt),
        saveData: connection.saveData ?? null
      } : null,
      navigation: navigation ? {
        type: navigation.type,
        responseEndMs: round(navigation.responseEnd),
        domInteractiveMs: round(navigation.domInteractive),
        domContentLoadedMs: round(navigation.domContentLoadedEventEnd),
        loadEventEndMs: round(navigation.loadEventEnd),
        transferSize: navigation.transferSize,
        encodedBodySize: navigation.encodedBodySize,
        decodedBodySize: navigation.decodedBodySize
      } : null,
      gpu: readGpu(renderCanvas)
    };
  };

  const runtimeSnapshot = module => {
    if (!module)
      return { available: false };
    try {
      return {
        available: true,
        wasmHeapBytes: module.HEAPU8?.buffer?.byteLength ?? null,
        pthread: module.PThread ? {
          runningWorkers: module.PThread.runningWorkers?.length ?? null,
          unusedWorkers: module.PThread.unusedWorkers?.length ?? null,
          pthreads: module.PThread.pthreads ? Object.keys(module.PThread.pthreads).length : null
        } : null,
        asyncify: module.Asyncify ? {
          state: module.Asyncify.state ?? null,
          activeExportCalls: module.Asyncify.exportCallStack?.length ?? null
        } : null
      };
    } catch (error) {
      return { available: false, error: errorDetails(error) };
    }
  };

  const flattenNumbers = (value, prefix, output) => {
    if (typeof value === "number" && Number.isFinite(value)) {
      (output[prefix] ||= []).push(value);
      return;
    }
    if (!value || typeof value !== "object" || Array.isArray(value))
      return;
    for (const [key, child] of Object.entries(value))
      flattenNumbers(child, prefix ? `${prefix}.${key}` : key, output);
  };
  const numericSummary = samples => {
    const series = {};
    for (const sample of samples) {
      flattenNumbers(sample.engine, "engine", series);
      flattenNumbers(sample.browser?.framePacing, "browser.framePacing", series);
      flattenNumbers(sample.browser?.memory, "browser.memory", series);
      flattenNumbers(sample.runtime, "runtime", series);
    }
    const summary = {};
    for (const [path, rawValues] of Object.entries(series)) {
      const values = [...rawValues].sort((a, b) => a - b);
      const mean = values.reduce((total, value) => total + value, 0) / values.length;
      summary[path] = {
        samples: values.length,
        minimum: round(values[0]),
        maximum: round(values[values.length - 1]),
        mean: round(mean),
        p50: round(percentile(values, 0.50)),
        p95: round(percentile(values, 0.95)),
        latest: round(rawValues[rawValues.length - 1])
      };
    }
    return summary;
  };

  window.ayaInstallDiagnostics = config => {
    if (!config || typeof config.getModule !== "function" || !config.engineExport)
      throw new TypeError("Invalid Aya diagnostics configuration");

    let activeRun = null;
    let cachedModule = null;
    let cachedEngineCall = null;

    const readEngine = module => {
      if (!module || typeof module.cwrap !== "function")
        return { available: false, error: { message: "Wasm runtime is not ready" } };
      try {
        if (cachedModule !== module) {
          cachedModule = module;
          cachedEngineCall = module.cwrap(config.engineExport, "string", []);
        }
        const payload = cachedEngineCall();
        return payload ? JSON.parse(payload) : {
          available: false,
          error: { message: "Engine returned an empty diagnostics payload" }
        };
      } catch (error) {
        return { available: false, error: errorDetails(error) };
      }
    };

    const readTarget = () => {
      if (typeof config.getTargetState !== "function")
        return null;
      try {
        const value = config.getTargetState();
        return value === undefined ? null : JSON.parse(JSON.stringify(value));
      } catch (error) {
        return { error: errorDetails(error) };
      }
    };

    const finishRun = (run, reason, captureFinalSample) => {
      if (run.finished)
        return run.report;
      if (captureFinalSample)
        run.capture(Math.min(run.durationMs, performance.now() - run.startTime), true);
      run.finished = true;
      clearTimeout(run.timer);
      cancelAnimationFrame(run.animationFrame);
      run.longTaskObserver?.disconnect();

      const endedAt = new Date();
      run.report.endedAt = endedAt.toISOString();
      run.report.actualDurationMs = round(performance.now() - run.startTime);
      run.report.completionReason = reason;
      run.report.sampling.actualSamples = run.report.samples.length;
      run.report.sampling.missedIntervals = reason === "completed"
        ? Math.max(run.missedIntervals,
          run.report.sampling.expectedSamples - run.report.samples.length)
        : run.missedIntervals;
      run.report.summary = {
        numericSeries: numericSummary(run.report.samples),
        engineUnavailableSamples: run.report.samples.filter(sample => !sample.engine?.available).length,
        hiddenSamples: run.report.samples.filter(sample => sample.browser?.document?.hidden).length,
        totalLongTasks: run.report.samples.reduce(
          (total, sample) => total + (sample.browser?.longTasks?.length || 0), 0)
      };

      window.ayaLastDiagnosis = run.report;
      window.ayaLastDiagnosisJson = JSON.stringify(run.report, null, 2);
      activeRun = null;
      console.log(`[Aya diagnose] Complete (${run.report.samples.length} samples). JSON follows:`);
      console.log(window.ayaLastDiagnosisJson);
      run.resolve(run.report);
      return run.report;
    };

    window.ayaStartDiagnose = (options = {}) => {
      if (activeRun) {
        console.warn("[Aya diagnose] A capture is already running.");
        return activeRun.promise;
      }

      const durationMs = Math.max(1000, Number(options.durationMs) || DEFAULT_DURATION_MS);
      const intervalMs = Math.max(250, Math.min(
        durationMs,
        Number(options.intervalMs) || DEFAULT_INTERVAL_MS
      ));
      const startedAt = new Date();
      const startTime = performance.now();
      const renderCanvas = typeof config.getRenderCanvas === "function"
        ? config.getRenderCanvas() : null;
      let resolvePromise;
      const promise = new Promise(resolve => {
        resolvePromise = resolve;
      });
      const run = {
        promise,
        resolve: resolvePromise,
        durationMs,
        intervalMs,
        startTime,
        timer: 0,
        animationFrame: 0,
        previousFrameTime: 0,
        frameDurations: [],
        frameWindowStart: startTime,
        longTasks: [],
        longTaskObserver: null,
        missedIntervals: 0,
        finished: false,
        report: {
          schema: "aya-engine-diagnostics-v1",
          target: config.target,
          buildId: config.buildId || null,
          startedAt: startedAt.toISOString(),
          requestedDurationMs: durationMs,
          environment: environmentSnapshot(config, renderCanvas),
          sampling: {
            intervalMs,
            expectedSamples: Math.ceil(durationMs / intervalMs) + 1,
            actualSamples: 0,
            missedIntervals: 0
          },
          samples: []
        }
      };

      const observeFrame = timestamp => {
        if (run.finished)
          return;
        if (run.previousFrameTime)
          run.frameDurations.push(timestamp - run.previousFrameTime);
        run.previousFrameTime = timestamp;
        run.animationFrame = requestAnimationFrame(observeFrame);
      };
      run.animationFrame = requestAnimationFrame(observeFrame);

      if (typeof PerformanceObserver === "function" &&
          PerformanceObserver.supportedEntryTypes?.includes("longtask")) {
        try {
          run.longTaskObserver = new PerformanceObserver(list => {
            for (const entry of list.getEntries()) {
              run.longTasks.push({
                offsetMs: round(entry.startTime - startTime),
                durationMs: round(entry.duration),
                name: entry.name || null
              });
            }
          });
          run.longTaskObserver.observe({ type: "longtask", buffered: false });
        } catch (error) {
          run.longTaskObserver = null;
        }
      }

      run.capture = (scheduledOffsetMs, finalSample = false) => {
        const now = performance.now();
        const module = config.getModule();
        const memory = performance.memory;
        const frameSpan = now - run.frameWindowStart;
        const sample = {
          index: run.report.samples.length,
          capturedAt: new Date().toISOString(),
          scheduledOffsetMs: round(scheduledOffsetMs),
          actualOffsetMs: round(now - startTime),
          schedulingDelayMs: round(Math.max(0, now - startTime - scheduledOffsetMs)),
          finalSample,
          browser: {
            document: {
              hidden: document.hidden,
              visibilityState: document.visibilityState,
              hasFocus: document.hasFocus()
            },
            viewport: {
              innerSize: [window.innerWidth, window.innerHeight],
              outerSize: [window.outerWidth, window.outerHeight],
              devicePixelRatio: window.devicePixelRatio,
              visualViewport: window.visualViewport ? {
                width: round(window.visualViewport.width),
                height: round(window.visualViewport.height),
                scale: round(window.visualViewport.scale),
                offsetLeft: round(window.visualViewport.offsetLeft),
                offsetTop: round(window.visualViewport.offsetTop)
              } : null
            },
            memory: memory ? {
              usedJsHeapBytes: memory.usedJSHeapSize,
              totalJsHeapBytes: memory.totalJSHeapSize,
              jsHeapLimitBytes: memory.jsHeapSizeLimit
            } : null,
            framePacing: framePacingSummary(run.frameDurations, frameSpan),
            longTasks: run.longTasks.splice(0),
            canvases: Array.from(document.querySelectorAll("canvas"), canvasDetails)
          },
          runtime: runtimeSnapshot(module),
          targetState: readTarget(),
          engine: readEngine(module)
        };
        run.frameDurations.length = 0;
        run.frameWindowStart = now;
        run.report.samples.push(sample);
      };

      const schedule = scheduledOffsetMs => {
        run.timer = setTimeout(() => {
          if (run.finished)
            return;
          const elapsed = performance.now() - startTime;
          const finalSample = elapsed >= durationMs || scheduledOffsetMs >= durationMs;
          run.capture(scheduledOffsetMs, finalSample);
          console.log(
            `[Aya diagnose] Sample ${run.report.samples.length}/${run.report.sampling.expectedSamples}` +
            ` at ${(elapsed / 1000).toFixed(1)}s`
          );
          if (finalSample) {
            finishRun(run, "completed", false);
            return;
          }

          const currentIndex = Math.round(scheduledOffsetMs / intervalMs);
          const nextIndex = Math.max(currentIndex + 1, Math.floor(elapsed / intervalMs) + 1);
          run.missedIntervals += Math.max(0, nextIndex - currentIndex - 1);
          const nextOffset = Math.min(durationMs, nextIndex * intervalMs);
          schedule(nextOffset);
        }, Math.max(0, startTime + scheduledOffsetMs - performance.now()));
      };

      activeRun = run;
      run.capture(0, false);
      console.log(
        `[Aya diagnose] Started ${config.target} capture for ${(durationMs / 1000).toFixed(0)}s` +
        ` at ${(intervalMs / 1000).toFixed(1)}s intervals.`
      );
      schedule(Math.min(intervalMs, durationMs));
      return promise;
    };

    window.ayaStopDiagnose = () => {
      if (!activeRun)
        return window.ayaLastDiagnosis || null;
      return finishRun(activeRun, "stopped", true);
    };

    console.info("[Aya diagnose] Ready. Run ayaStartDiagnose() to capture two minutes of diagnostics.");
  };
})();
