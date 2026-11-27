-- Copyright 2026 The Chromium Authors
-- Use of this source code is governed by a BSD-style license that can be
-- found in the LICENSE file.

-- Computes Android WebView scroll jank and input-to-present latency metrics.
--
-- Combines:
--   1. Host embedder SurfaceFlinger FrameTimeline jank rates across the active
--      scroll window (`crossbench-web-power-start` to
--      `crossbench-web-power-stop`, falling back to trace bounds).
--   2. The 4-step WebView scroll-update to FrameTimeline join:
--      `chrome_event_latencies.display_trace_id`
--        -> `chrome_graphics_pipeline_display_frame_steps`
--           (`STEP_BUFFER_SWAP_POST_SUBMIT` on the host RenderThread)
--        -> enclosing `android_frames_draw_frame` (`DrawFrames <vsync_id>`)
--        -> `actual_frame_timeline_slice` (`surface_frame_token = <vsync_id>`).
--   3. Chromium's `chrome_scroll_jank_v4_results` delayed-frame rate when
--      ScrollJankV4 trace events are present.

INCLUDE PERFETTO MODULE android.frames.jank_type;
INCLUDE PERFETTO MODULE android.frames.timeline;
INCLUDE PERFETTO MODULE chrome.event_latency;
INCLUDE PERFETTO MODULE chrome.graphics_pipeline;
INCLUDE PERFETTO MODULE chrome.scroll_jank_v4;

WITH
  scroll_marks AS (
    SELECT
      MIN(
        IIF(name = 'crossbench-web-power-start', ts, NULL)
      ) AS start_mark_ts,
      MAX(
        IIF(name = 'crossbench-web-power-stop', ts, NULL)
      ) AS stop_mark_ts
    FROM slice
    WHERE
      name IN ('crossbench-web-power-start', 'crossbench-web-power-stop')
  ),
  scroll_window AS (
    SELECT
      COALESCE(start_mark_ts, trace_start()) AS start_ts,
      CASE
        WHEN
          stop_mark_ts IS NOT NULL
          AND stop_mark_ts > COALESCE(start_mark_ts, trace_start())
          THEN stop_mark_ts
        ELSE trace_end()
      END AS end_ts
    FROM scroll_marks
    WHERE
      trace_end() > COALESCE(start_mark_ts, trace_start())
  ),
  wv_host_upids AS (
    SELECT DISTINCT
      t.upid
    FROM chrome_graphics_pipeline_display_frame_steps AS gp
    INNER JOIN thread AS t
      USING (utid)
    WHERE
      gp.step = 'STEP_BUFFER_SWAP_POST_SUBMIT'
      AND t.upid IS NOT NULL
    UNION
    SELECT DISTINCT
      t.upid
    FROM slice AS s
    INNER JOIN thread_track AS tt
      ON tt.id = s.track_id
    INNER JOIN thread AS t
      USING (utid)
    WHERE
      t.upid IS NOT NULL
      AND (
        s.category GLOB '*android_webview*'
        OR s.name GLOB 'WebView*'
        OR s.name GLOB '*AwDrawFn*'
        OR s.name GLOB '*AwContents*'
      )
    UNION
    SELECT DISTINCT
      p.upid
    FROM process AS p
    WHERE
      p.name GLOB '*webview*'
      AND p.name NOT GLOB '*sandboxed_process*'
  ),
  wv_host_frames AS (
    SELECT
      aft.upid,
      aft.surface_frame_token,
      MAX(
        IIF(android_is_missed_frame_type(aft.jank_type), 1, 0)
      ) AS is_missed_frame,
      MAX(
        IIF(android_is_app_jank_type(aft.jank_type), 1, 0)
      ) AS is_app_jank,
      MAX(aft.ts + aft.dur) AS app_present_ts,
      MAX(aft.display_frame_token) AS display_frame_token
    FROM actual_frame_timeline_slice AS aft
    CROSS JOIN scroll_window AS sw
    INNER JOIN wv_host_upids AS host
      ON host.upid = aft.upid
    WHERE
      aft.surface_frame_token != 0
      AND aft.dur >= 0
      AND aft.ts >= sw.start_ts
      AND aft.ts < sw.end_ts
    GROUP BY
      aft.upid,
      aft.surface_frame_token
  ),
  sf_display_frames AS (
    SELECT
      aft.display_frame_token,
      MAX(aft.ts + aft.dur) AS sf_present_ts
    FROM actual_frame_timeline_slice AS aft
    WHERE
      COALESCE(aft.surface_frame_token, 0) = 0
      AND aft.display_frame_token != 0
      AND aft.dur >= 0
    GROUP BY
      aft.display_frame_token
  ),
  wv_scroll_updates AS (
    SELECT
      el.id AS event_latency_id,
      el.ts AS input_ts,
      el.display_trace_id
    FROM chrome_event_latencies AS el
    CROSS JOIN scroll_window AS sw
    WHERE
      el.is_presented = 1
      AND el.display_trace_id IS NOT NULL
      AND el.event_type IN (
        'FIRST_GESTURE_SCROLL_UPDATE',
        'GESTURE_SCROLL_UPDATE',
        'INERTIAL_GESTURE_SCROLL_UPDATE'
      )
      AND el.ts >= sw.start_ts
      AND el.ts < sw.end_ts
  ),
  wv_buffer_swaps AS (
    SELECT
      gp.display_trace_id,
      MIN(df.upid) AS upid,
      MIN(df.frame_id) AS surface_frame_token
    FROM chrome_graphics_pipeline_display_frame_steps AS gp
    INNER JOIN android_frames_draw_frame AS df
      ON df.render_thread_utid = gp.utid
    INNER JOIN slice AS ds
      ON ds.id = df.id
    WHERE
      gp.step = 'STEP_BUFFER_SWAP_POST_SUBMIT'
      AND gp.display_trace_id IS NOT NULL
      AND ds.dur > 0
      AND gp.ts >= ds.ts
      AND gp.ts < ds.ts + ds.dur
    GROUP BY
      gp.display_trace_id
  ),
  wv_linked_scroll_updates AS (
    SELECT
      su.event_latency_id,
      su.input_ts,
      hf.upid,
      hf.surface_frame_token,
      hf.is_missed_frame,
      COALESCE(sf.sf_present_ts, hf.app_present_ts) AS present_ts
    FROM wv_scroll_updates AS su
    INNER JOIN wv_buffer_swaps AS bs
      ON bs.display_trace_id = su.display_trace_id
    INNER JOIN wv_host_frames AS hf
      ON
        hf.upid = bs.upid
        AND hf.surface_frame_token = bs.surface_frame_token
    LEFT JOIN sf_display_frames AS sf
      ON sf.display_frame_token = hf.display_frame_token
  ),
  wv_linked_scroll_frames AS (
    SELECT
      upid,
      surface_frame_token,
      MAX(is_missed_frame) AS is_missed_frame
    FROM wv_linked_scroll_updates
    GROUP BY
      upid,
      surface_frame_token
  ),
  frametimeline_summary AS (
    SELECT
      COUNT(*) AS frames_count,
      CASE
        WHEN COUNT(*) > 0
          THEN 100.0 * SUM(is_missed_frame) / COUNT(*)
        ELSE NULL
      END AS missed_frames_percentage,
      CASE
        WHEN COUNT(*) > 0
          THEN 100.0 * SUM(is_app_jank) / COUNT(*)
        ELSE NULL
      END AS app_jank_percentage
    FROM wv_host_frames
  ),
  linked_scroll_summary AS (
    SELECT
      (
        SELECT
          CASE
            WHEN COUNT(*) > 0
              THEN 100.0 * SUM(is_missed_frame) / COUNT(*)
            ELSE NULL
          END
        FROM wv_linked_scroll_frames
      ) AS scroll_update_missed_frames_percentage,
      (
        SELECT
          AVG((present_ts - input_ts) / 1e6)
        FROM wv_linked_scroll_updates
        WHERE
          present_ts > input_ts
      ) AS input_to_sf_present_mean_ms
  ),
  scroll_jank_v4_summary AS (
    SELECT
      COUNT(*) AS frames_count,
      CASE
        WHEN COUNT(*) > 0
          THEN 100.0 * SUM(IIF(COALESCE(v4.is_janky, 0) != 0, 1, 0)) / COUNT(*)
        ELSE NULL
      END AS delayed_frames_percentage
    FROM chrome_scroll_jank_v4_results AS v4
    CROSS JOIN scroll_window AS sw
    WHERE
      v4.ts >= sw.start_ts
      AND v4.ts < sw.end_ts
  )
SELECT
  ft.missed_frames_percentage AS wv_frametimeline_missed_frames_percentage,
  ft.app_jank_percentage AS wv_frametimeline_app_jank_percentage,
  ft.frames_count AS wv_frametimeline_frames_count,
  ls.scroll_update_missed_frames_percentage
    AS wv_scroll_update_frametimeline_missed_frames_percentage,
  v4.delayed_frames_percentage AS wv_scroll_jank_v4_delayed_frames_percentage,
  ls.input_to_sf_present_mean_ms AS wv_scroll_input_to_sf_present_mean_ms
FROM frametimeline_summary AS ft
CROSS JOIN linked_scroll_summary AS ls
CROSS JOIN scroll_jank_v4_summary AS v4
WHERE
  ft.frames_count > 0
  OR v4.frames_count > 0;
