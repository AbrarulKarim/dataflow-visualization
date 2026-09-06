"""Regenerate the example networks. Doubles as a tour of the builder API.

    python examples/make_examples.py
"""

from __future__ import annotations

from pathlib import Path

from dataflow.model import GraphBuilder, save_graph

HERE = Path(__file__).parent


def chain() -> GraphBuilder:
    """A slow source feeding a three-stage pipeline, one stage per sleep policy."""
    b = GraphBuilder("pipeline")
    b.source("src", interval=12, exec_time=1, position=(80, 200))
    b.actor("decode", exec_time=3, sleep="immediate", sleep_delay=2, wakeup_delay=3,
            exec_power=1.4, idle_power=0.4, sleep_power=0.02, position=(300, 200))
    b.actor("filter", exec_time=4, sleep="timeout", timeout=6, sleep_delay=3,
            wakeup_delay=5, exec_power=1.1, idle_power=0.35, position=(520, 200))
    b.actor("encode", exec_time=2, sleep="never",
            exec_power=0.9, idle_power=0.3, position=(740, 200))
    b.sink("snk", exec_time=1, position=(960, 200))
    b.connect("src", "decode", capacity=8)
    b.connect("decode", "filter", capacity=8)
    b.connect("filter", "encode", capacity=8)
    b.connect("encode", "snk", capacity=8)
    return b


def feedback() -> GraphBuilder:
    """A self-timed cycle: throughput is set by the loop, not by the source."""
    b = GraphBuilder("feedback-loop")
    b.source("src", interval=5, exec_time=1, position=(80, 240))
    b.actor("split", exec_time=2, sleep="timeout", timeout=4, position=(300, 240))
    b.actor("work", exec_time=3, sleep="immediate", position=(540, 160))
    b.actor("merge", exec_time=2, sleep="timeout", timeout=8, position=(780, 240))
    b.sink("snk", exec_time=1, position=(1000, 240))
    b.connect("src", "split", capacity=6)
    b.connect("split", "work", capacity=4)
    b.connect("work", "merge", capacity=4)
    b.connect("merge", "snk", capacity=6)
    # Credit loop: `split` may only run while `merge` has returned a token.
    b.connect("merge", "split", capacity=3, initial_tokens=2, channel_id="credit")
    return b


def csdf() -> GraphBuilder:
    """A phased-rate stage: consumes 1,3 and produces 2,0 on alternating phases."""
    b = GraphBuilder("cyclo-static")
    b.source("src", interval=3, distribution="gaussian", stddev=1.0, exec_time=1,
             position=(80, 200))
    b.actor("resample", kind="phased_rate", phases=2, exec_time=2, sleep="timeout",
            timeout=5, position=(340, 200))
    b.actor("post", exec_time=1, sleep="immediate", position=(600, 200))
    b.sink("snk", exec_time=1, position=(840, 200))
    b.connect("src", "resample", consume=[1, 3], capacity=12)
    b.connect("resample", "post", produce=[2, 0], capacity=12)
    b.connect("post", "snk", capacity=12)
    return b


# --------------------------------------------------------------------------- #
# Real-world networks
#
# Rates are the actual token counts of each application (macroblock rows, OFDM
# subcarriers, range gates); execution times and powers are in the framework's
# own relative units, chosen so the balance between stages resembles a real
# implementation rather than claiming to be a measurement of one.
# --------------------------------------------------------------------------- #


def h264_decoder() -> GraphBuilder:
    """H.264 baseline decode of a QCIF stream, at macroblock-row granularity.

    A frame is 9 rows of macroblocks. Motion compensation needs the previous
    frame, which is the reference loop back from the deblocking filter -- it
    starts with a full frame of credits so the first frame can decode. That loop
    is what stops the pipeline running arbitrarily far ahead of itself.
    """
    b = GraphBuilder("h264-decoder")
    b.source("bitstream", interval=1200, exec_time=6, distribution="gaussian",
             stddev=90, exec_power=0.5, idle_power=0.2, position=(80, 260))
    b.actor("nal_parse", exec_time=40, sleep="timeout", timeout=150,
            exec_power=0.9, idle_power=0.35, position=(300, 260))
    b.actor("cabac", exec_time=30, sleep="never",
            exec_power=1.8, idle_power=0.6, position=(520, 260))
    b.actor("iq_idct", exec_time=25, sleep="timeout", timeout=60,
            exec_power=1.3, idle_power=0.4, position=(740, 260))
    b.actor("motion_comp", exec_time=35, sleep="timeout", timeout=90,
            exec_power=1.6, idle_power=0.5, sleep_delay=8, wakeup_delay=20,
            position=(960, 260))
    b.actor("deblock", exec_time=20, sleep="immediate", sleep_delay=6,
            wakeup_delay=14, exec_power=1.1, idle_power=0.35, position=(1180, 260))
    b.sink("display", exec_time=8, exec_power=0.4, idle_power=0.15,
           position=(1400, 260))

    b.connect("bitstream", "nal_parse", capacity=3)
    b.connect("nal_parse", "cabac", produce=9, capacity=18)      # 9 rows per frame
    b.connect("cabac", "iq_idct", capacity=12)
    b.connect("iq_idct", "motion_comp", capacity=12)
    # Deblocking reassembles a frame, so it takes all 9 rows in one firing.
    b.connect("motion_comp", "deblock", consume=9, capacity=12)
    b.connect("deblock", "display", capacity=3)
    # Reference frame: deblock hands 9 rows back, motion compensation takes one
    # per row. One frame of credits is available at reset.
    b.connect("deblock", "motion_comp", produce=9, capacity=18, initial_tokens=9,
              channel_id="reference")
    return b


def ofdm_receiver() -> GraphBuilder:
    """802.11a/g baseband receive chain, one OFDM symbol per iteration.

    Token counts follow the standard: 80 samples per symbol (64 plus a 16-sample
    cyclic prefix), 48 data subcarriers and 4 pilots after equalisation, 4 bits
    per 16-QAM symbol, and a rate-1/2 convolutional code. Channel estimation runs
    off the pilots and feeds back into the equaliser.

    Viterbi is the hotspot, running at roughly 90% occupancy against the 80-cycle
    symbol period: push its execution time past 80 and it becomes the bottleneck,
    and backpressure throttles the whole chain behind it.
    """
    # Laid out as a serpentine: eleven stages in a straight line would run off
    # any screen.
    b = GraphBuilder("ofdm-receiver")
    b.source("rf_adc", interval=80, exec_time=4, exec_power=0.9, idle_power=0.5,
             position=(80, 180))
    b.actor("sync", exec_time=20, sleep="never",
            exec_power=1.2, idle_power=0.45, position=(300, 180))
    b.actor("remove_cp", kind="static_rate", exec_time=8, sleep="timeout", timeout=40,
            exec_power=0.5, idle_power=0.2, position=(520, 180))
    b.actor("fft64", exec_time=40, sleep="timeout", timeout=30,
            exec_power=2.2, idle_power=0.6, sleep_delay=5, wakeup_delay=12,
            position=(740, 180))
    b.actor("equalize", exec_time=16, sleep="timeout", timeout=40,
            exec_power=1.1, idle_power=0.4, position=(960, 180))
    b.actor("chan_est", exec_time=22, sleep="timeout", timeout=60,
            exec_power=0.8, idle_power=0.3, position=(1180, 60))
    b.actor("demap", exec_time=24, sleep="timeout", timeout=40,
            exec_power=0.9, idle_power=0.3, position=(1180, 400))
    b.actor("deinterleave", exec_time=12, sleep="immediate", sleep_delay=4,
            wakeup_delay=10, exec_power=0.4, idle_power=0.15, position=(960, 400))
    b.actor("viterbi", exec_time=70, sleep="timeout", timeout=25,
            exec_power=2.8, idle_power=0.8, sleep_delay=10, wakeup_delay=25,
            position=(740, 400))
    b.actor("descramble", exec_time=6, sleep="immediate",
            exec_power=0.3, idle_power=0.12, position=(520, 400))
    b.sink("mac_out", exec_time=4, exec_power=0.3, idle_power=0.12,
           position=(300, 400))

    b.connect("rf_adc", "sync", produce=80, consume=80, capacity=240)
    b.connect("sync", "remove_cp", produce=80, consume=80, capacity=240)
    b.connect("remove_cp", "fft64", produce=64, consume=64, capacity=192)
    b.connect("fft64", "equalize", produce=64, consume=64, capacity=192)
    b.connect("equalize", "demap", produce=48, consume=48, capacity=144)
    b.connect("demap", "deinterleave", produce=192, consume=192, capacity=576)
    b.connect("deinterleave", "viterbi", produce=192, consume=192, capacity=576)
    b.connect("viterbi", "descramble", produce=96, consume=96, capacity=288)
    b.connect("descramble", "mac_out", produce=96, consume=96, capacity=288)
    # Pilot-driven channel tracking: 4 pilots out, one correction back.
    b.connect("equalize", "chan_est", produce=4, consume=4, capacity=12)
    b.connect("chan_est", "equalize", produce=1, consume=1, capacity=3,
              initial_tokens=1, channel_id="chan_corr")
    return b


def sensor_node() -> GraphBuilder:
    """A duty-cycled wireless sensor node -- the case sleep policies exist for.

    Events are rare and bursty, so every stage spends most of its life waiting.
    The radio is the interesting one: transmitting is expensive and waking it is
    slow, so whether it should sleep between events is a real trade-off rather
    than an obvious win. Compare the energy with the radio's policy set to
    `never`.
    """
    b = GraphBuilder("sensor-node")
    b.source("event", interval=4000, distribution="exponential", exec_time=2,
             exec_power=0.2, idle_power=0.05, position=(80, 220))
    b.actor("adc", exec_time=12, sleep="immediate", sleep_delay=3, wakeup_delay=8,
            exec_power=0.6, idle_power=0.25, sleep_power=0.01, position=(300, 220))
    b.actor("fir_filter", exec_time=60, sleep="timeout", timeout=200,
            exec_power=1.4, idle_power=0.45, sleep_power=0.02,
            sleep_delay=6, wakeup_delay=18, position=(520, 220))
    b.actor("features", exec_time=90, sleep="timeout", timeout=120,
            exec_power=1.7, idle_power=0.5, sleep_power=0.02,
            sleep_delay=6, wakeup_delay=20, position=(740, 220))
    b.actor("classify", exec_time=240, sleep="immediate", sleep_delay=10,
            wakeup_delay=45, exec_power=3.2, idle_power=0.9, sleep_power=0.03,
            position=(960, 220))
    # Radios draw a lot and wake slowly; this is the stage worth arguing about.
    b.actor("radio_tx", exec_time=150, sleep="immediate", sleep_delay=40,
            wakeup_delay=400, exec_power=9.0, idle_power=2.4, sleep_power=0.05,
            position=(1180, 220))
    b.sink("gateway", exec_time=5, exec_power=0.2, idle_power=0.05,
           position=(1400, 220))

    b.connect("event", "adc", capacity=4)
    b.connect("adc", "fir_filter", capacity=4)
    b.connect("fir_filter", "features", capacity=4)
    b.connect("features", "classify", capacity=4)
    b.connect("classify", "radio_tx", capacity=4)
    b.connect("radio_tx", "gateway", capacity=4)
    return b


def radar_doppler() -> GraphBuilder:
    """Pulse-Doppler radar front end, built around a cyclo-static corner turn.

    The corner turn is the textbook phased-rate actor: it takes one pulse of 64
    range gates on each of four phases and emits the assembled 4x64 block on the
    last one, so the Doppler FFT sees range-Doppler matrices rather than pulses.
    """
    b = GraphBuilder("radar-doppler")
    b.source("rx_array", interval=250, exec_time=10, exec_power=1.0,
             idle_power=0.4, position=(80, 240))
    b.actor("pulse_compress", exec_time=45, sleep="timeout", timeout=80,
            exec_power=2.0, idle_power=0.6, position=(320, 240))
    b.actor("corner_turn", kind="phased_rate", phases=4, exec_time=12,
            sleep="timeout", timeout=200, exec_power=0.7, idle_power=0.3,
            position=(560, 240))
    b.actor("doppler_fft", exec_time=120, sleep="timeout", timeout=100,
            exec_power=2.6, idle_power=0.7, sleep_delay=8, wakeup_delay=22,
            position=(800, 240))
    b.actor("cfar", exec_time=30, sleep="immediate", sleep_delay=5,
            wakeup_delay=12, exec_power=1.2, idle_power=0.4, position=(1040, 240))
    b.sink("tracker", exec_time=15, exec_power=0.4, idle_power=0.15,
           position=(1280, 240))

    b.connect("rx_array", "pulse_compress", produce=64, consume=64, capacity=256)
    b.connect("pulse_compress", "corner_turn", produce=64, consume=64, capacity=256)
    # Four phases in, one block out: nothing on the first three, 4x64 on the last.
    b.connect("corner_turn", "doppler_fft", produce=[0, 0, 0, 256], consume=256,
              capacity=768)
    b.connect("doppler_fft", "cfar", produce=256, consume=256, capacity=768)
    b.connect("cfar", "tracker", produce=4, consume=4, capacity=16)
    return b


def hevc_encoder() -> GraphBuilder:
    """HEVC encode at CTU granularity -- a fork-join inside two nested loops.

    Sixteen coding tree units per frame. Each one is tried both ways at once:
    ``motion_est`` against the reference frame and ``intra_pred`` from its own
    neighbours, and ``mode_decide`` joins the two branches and picks. Two loops
    close over that forward path:

    * the **reconstruction loop** -- quantise, invert it all again, filter, and
      park the result in the decoded picture buffer, which is what motion
      estimation searches next frame. It is why an encoder cannot run ahead of
      itself: frame N+1 cannot be searched until frame N is rebuilt;
    * the **rate-control loop** -- the entropy coder reports how many bits the
      frame actually cost and the quantiser parameter for the next one is set
      from it.

    The forward path runs left to right along the top, the reconstruction loop
    returns along the bottom, and rate control arcs over the top.
    """
    b = GraphBuilder("hevc-encoder")
    b.source("raw_video", interval=4000, exec_time=20, distribution="gaussian",
             stddev=60, exec_power=0.6, idle_power=0.25, position=(80, 220))
    b.actor("ctu_split", exec_time=30, sleep="timeout", timeout=300,
            exec_power=0.7, idle_power=0.3, position=(280, 220))
    # Motion estimation is the classic encoder hotspot.
    b.actor("motion_est", exec_time=120, sleep="timeout", timeout=200,
            exec_power=3.4, idle_power=0.9, sleep_delay=10, wakeup_delay=30,
            position=(520, 220))
    b.actor("intra_pred", exec_time=45, sleep="timeout", timeout=200,
            exec_power=1.5, idle_power=0.5, position=(520, 70))
    b.actor("mode_decide", exec_time=35, sleep="timeout", timeout=150,
            exec_power=1.3, idle_power=0.45, position=(760, 220))
    b.actor("transform", exec_time=28, sleep="timeout", timeout=150,
            exec_power=1.2, idle_power=0.4, position=(960, 220))
    b.actor("quantize", exec_time=18, sleep="timeout", timeout=150,
            exec_power=0.8, idle_power=0.3, position=(1160, 220))
    b.actor("entropy", exec_time=140, sleep="timeout", timeout=400,
            exec_power=2.1, idle_power=0.7, position=(1400, 220))
    b.actor("rate_ctrl", exec_time=25, sleep="immediate", sleep_delay=5,
            wakeup_delay=15, exec_power=0.5, idle_power=0.2, position=(1310, 60))
    b.sink("bitstream", exec_time=12, exec_power=0.4, idle_power=0.15,
           position=(1620, 220))

    # Reconstruction path, laid out right to left underneath.
    b.actor("dequant", exec_time=16, sleep="timeout", timeout=150,
            exec_power=0.7, idle_power=0.28, position=(1160, 440))
    b.actor("itransform", exec_time=28, sleep="timeout", timeout=150,
            exec_power=1.2, idle_power=0.4, position=(980, 440))
    b.actor("reconstruct", exec_time=22, sleep="timeout", timeout=150,
            exec_power=0.9, idle_power=0.35, position=(800, 440))
    b.actor("deblock", exec_time=34, sleep="timeout", timeout=150,
            exec_power=1.1, idle_power=0.4, position=(620, 440))
    b.actor("sao", exec_time=26, sleep="timeout", timeout=150,
            exec_power=0.9, idle_power=0.35, position=(440, 440))
    b.actor("dpb", exec_time=40, sleep="timeout", timeout=500,
            exec_power=0.6, idle_power=0.5, position=(260, 440))

    b.connect("raw_video", "ctu_split", capacity=3)
    # Fork: every CTU is tried both inter and intra.
    b.connect("ctu_split", "motion_est", produce=16, capacity=40)
    b.connect("ctu_split", "intra_pred", produce=16, capacity=40)
    # Join: the mode decision needs both candidates for the same CTU.
    b.connect("motion_est", "mode_decide", capacity=24)
    b.connect("intra_pred", "mode_decide", capacity=24)
    b.connect("mode_decide", "transform", capacity=24)
    b.connect("transform", "quantize", capacity=24)
    # Fork again: coefficients go to the entropy coder and back round the
    # reconstruction loop.
    b.connect("quantize", "entropy", consume=16, capacity=40)
    b.connect("quantize", "dequant", capacity=24)
    b.connect("entropy", "bitstream", capacity=3)

    b.connect("dequant", "itransform", capacity=24)
    b.connect("itransform", "reconstruct", capacity=24)
    b.connect("reconstruct", "deblock", capacity=24)
    b.connect("deblock", "sao", capacity=24)
    b.connect("sao", "dpb", consume=16, capacity=40)
    # The reference frame: one frame of CTUs is available at reset.
    b.connect("dpb", "motion_est", produce=16, capacity=40, initial_tokens=16,
              channel_id="reference")
    # Rate control: bits spent on this frame set the quantiser for the next.
    b.connect("entropy", "rate_ctrl", capacity=3)
    b.connect("rate_ctrl", "quantize", produce=16, capacity=40, initial_tokens=16,
              channel_id="qp")
    return b


def turbo_decoder() -> GraphBuilder:
    """An LTE turbo decoder: two soft-in soft-out decoders trading beliefs.

    The whole point is the loop. Each code block goes round eight times, the two
    MAP decoders passing extrinsic information through the interleaver and back
    again, before a hard decision is emitted. The iteration count is expressed in
    the rates rather than in control flow: both decoders are phased-rate actors
    with eight phases, `siso_a` taking the block in on its first phase only and
    `siso_b` emitting a decision on its last. One token sits in the feedback loop
    at reset to prime the first half-iteration.

    This is what a fixed-iteration decoder looks like in static dataflow; a
    stopping rule that reacts to the CRC needs the dynamic actors, which are not
    implemented yet.
    """
    b = GraphBuilder("turbo-decoder")
    b.source("channel_llr", interval=900, exec_time=25, exec_power=0.8,
             idle_power=0.3, position=(80, 230))
    b.actor("siso_a", kind="phased_rate", phases=8, exec_time=40,
            sleep="timeout", timeout=120, exec_power=3.0, idle_power=0.8,
            sleep_delay=8, wakeup_delay=20, position=(340, 230))
    b.actor("interleave", exec_time=10, sleep="timeout", timeout=120,
            exec_power=0.5, idle_power=0.2, position=(600, 110))
    b.actor("siso_b", kind="phased_rate", phases=8, exec_time=40,
            sleep="timeout", timeout=120, exec_power=3.0, idle_power=0.8,
            sleep_delay=8, wakeup_delay=20, position=(860, 230))
    b.actor("deinterleave", exec_time=10, sleep="timeout", timeout=120,
            exec_power=0.5, idle_power=0.2, position=(600, 350))
    b.actor("crc_check", exec_time=18, sleep="immediate", sleep_delay=4,
            wakeup_delay=12, exec_power=0.6, idle_power=0.25, position=(1120, 230))
    b.sink("transport", exec_time=8, exec_power=0.3, idle_power=0.12,
           position=(1340, 230))

    # The block is taken in once, on the first of the eight phases.
    b.connect("channel_llr", "siso_a", consume=[1, 0, 0, 0, 0, 0, 0, 0], capacity=4)
    b.connect("siso_a", "interleave", capacity=4)
    b.connect("interleave", "siso_b", capacity=4)
    b.connect("siso_b", "deinterleave", capacity=4)
    # The half-iteration that closes the loop, primed with one token.
    b.connect("deinterleave", "siso_a", capacity=4, initial_tokens=1,
              channel_id="extrinsic")
    # A hard decision only on the last phase.
    b.connect("siso_b", "crc_check", produce=[0, 0, 0, 0, 0, 0, 0, 1], capacity=4)
    b.connect("crc_check", "transport", capacity=4)
    return b


def echo_canceller() -> GraphBuilder:
    """Acoustic echo cancellation: two live inputs and an adaptation loop.

    Two sources rather than one -- the far-end signal being played and the
    microphone hearing it back -- and they join at the subtractor. The loop is
    the LMS coefficient update: the residual error and the far-end reference go
    back into the filter's taps, so what the filter does next depends on how
    wrong it was last time. The initial token on that loop is the starting set
    of coefficients.
    """
    b = GraphBuilder("echo-canceller")
    b.source("far_end", interval=128, exec_time=6, exec_power=0.4,
             idle_power=0.15, position=(80, 130))
    b.source("near_mic", interval=128, exec_time=6, exec_power=0.4,
             idle_power=0.15, position=(80, 360))
    b.actor("adaptive_filter", exec_time=55, sleep="timeout", timeout=100,
            exec_power=2.4, idle_power=0.7, sleep_delay=6, wakeup_delay=16,
            position=(340, 130))
    b.actor("subtract", exec_time=8, sleep="timeout", timeout=100,
            exec_power=0.4, idle_power=0.18, position=(600, 245))
    b.actor("lms_update", exec_time=48, sleep="timeout", timeout=100,
            exec_power=2.1, idle_power=0.6, sleep_delay=6, wakeup_delay=16,
            position=(340, 380))
    b.actor("nlp", exec_time=20, sleep="immediate", sleep_delay=4,
            wakeup_delay=12, exec_power=0.7, idle_power=0.25, position=(840, 245))
    b.sink("uplink", exec_time=6, exec_power=0.3, idle_power=0.12,
           position=(1060, 245))

    b.connect("far_end", "adaptive_filter", capacity=6)
    # The filter passes the far-end reference on to the update alongside its
    # echo estimate.
    b.connect("adaptive_filter", "subtract", capacity=6, channel_id="echo_est")
    b.connect("adaptive_filter", "lms_update", capacity=6, channel_id="far_ref")
    b.connect("near_mic", "subtract", capacity=6)
    b.connect("subtract", "nlp", capacity=6)
    b.connect("subtract", "lms_update", capacity=6, channel_id="error")
    # Adaptation loop, primed with the initial coefficient set.
    b.connect("lms_update", "adaptive_filter", capacity=4, initial_tokens=1,
              channel_id="coefficients")
    b.connect("nlp", "uplink", capacity=6)
    return b


def slam_frontend() -> GraphBuilder:
    """Visual-inertial SLAM front end: two sensors, two loops of different length.

    The camera and the IMU are independent sources running at different rates,
    joining at the pose estimator. Two loops close over it:

    * a **tight, per-frame loop** -- feature matching needs the previous frame's
      descriptors, so the track carries one frame of delay;
    * a **slow outer loop** -- loop-closure detection runs over a batch of eight
      keyframes and, when it fires, corrects the map that pose estimation is
      working against.

    The two loops run at rates eight apart, which is the interesting part: the
    map has to take its correction on the same eighth-keyframe cadence the outer
    loop produces it on, or the graph is rate-inconsistent and deadlocks.
    """
    b = GraphBuilder("slam-frontend")
    b.source("camera", interval=1000, exec_time=25, exec_power=0.8,
             idle_power=0.3, position=(80, 240))
    b.source("imu", interval=125, exec_time=4, exec_power=0.2,
             idle_power=0.08, position=(80, 470))
    b.actor("undistort", exec_time=60, sleep="timeout", timeout=200,
            exec_power=1.3, idle_power=0.45, position=(300, 240))
    b.actor("detect", exec_time=180, sleep="timeout", timeout=200,
            exec_power=2.9, idle_power=0.8, sleep_delay=8, wakeup_delay=24,
            position=(520, 240))
    b.actor("describe", exec_time=90, sleep="timeout", timeout=200,
            exec_power=1.8, idle_power=0.55, position=(740, 240))
    b.actor("match", exec_time=110, sleep="timeout", timeout=200,
            exec_power=2.2, idle_power=0.6, position=(960, 240))
    # The IMU is pre-integrated between camera frames: 8 samples per frame.
    b.actor("imu_preint", exec_time=12, sleep="timeout", timeout=300,
            exec_power=0.4, idle_power=0.15, position=(520, 470))
    b.actor("pose_est", exec_time=140, sleep="timeout", timeout=250,
            exec_power=2.6, idle_power=0.75, sleep_delay=8, wakeup_delay=22,
            position=(1180, 350))
    # Phased over the same eight keyframes as loop closure: it takes a map
    # correction on the eighth and nothing on the other seven. Consuming one
    # every frame would be rate-inconsistent against a path that only produces
    # one every eighth, and the graph would deadlock on the second keyframe.
    b.actor("local_map", kind="phased_rate", phases=8, exec_time=70,
            sleep="timeout", timeout=400,
            exec_power=1.1, idle_power=0.5, position=(1400, 350))
    # Loop closure only wakes every eighth keyframe.
    b.actor("loop_closure", kind="phased_rate", phases=8, exec_time=260,
            sleep="immediate", sleep_delay=12, wakeup_delay=60,
            exec_power=3.6, idle_power=0.9, position=(1180, 130))
    b.actor("pose_graph", exec_time=320, sleep="immediate", sleep_delay=12,
            wakeup_delay=60, exec_power=3.1, idle_power=0.85, position=(960, 60))
    b.sink("trajectory", exec_time=10, exec_power=0.3, idle_power=0.12,
           position=(1620, 350))

    b.connect("camera", "undistort", capacity=3)
    b.connect("undistort", "detect", capacity=3)
    b.connect("detect", "describe", capacity=3)
    b.connect("describe", "match", capacity=3, channel_id="descriptors")
    # Tight loop: matching compares against the previous frame, so the track is
    # delayed by exactly one frame.
    b.connect("match", "match", capacity=2, initial_tokens=1, channel_id="prev_frame")
    b.connect("imu", "imu_preint", consume=8, capacity=24)
    b.connect("imu_preint", "pose_est", capacity=3)
    b.connect("match", "pose_est", capacity=3)
    b.connect("pose_est", "local_map", capacity=3)
    b.connect("local_map", "trajectory", capacity=3)
    # Slow outer loop: eight keyframes in, one correction out, and the map is
    # already usable at reset.
    b.connect("local_map", "loop_closure", capacity=12)
    b.connect("loop_closure", "pose_graph", produce=[0, 0, 0, 0, 0, 0, 0, 1],
              capacity=3)
    b.connect("pose_graph", "local_map", consume=[0, 0, 0, 0, 0, 0, 0, 1],
              capacity=3, initial_tokens=1, channel_id="map_correction")
    return b


EXAMPLES = {
    "chain": chain,
    "feedback": feedback,
    "phased": csdf,
    "h264-decoder": h264_decoder,
    "ofdm-receiver": ofdm_receiver,
    "sensor-node": sensor_node,
    "radar-doppler": radar_doppler,
    "hevc-encoder": hevc_encoder,
    "turbo-decoder": turbo_decoder,
    "echo-canceller": echo_canceller,
    "slam-frontend": slam_frontend,
}


def main() -> None:
    for name, factory in EXAMPLES.items():
        graph = factory().build()
        path = HERE / f"{name}.dfg.json"
        save_graph(graph, path)
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
