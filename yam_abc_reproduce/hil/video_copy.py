"""Packet-copy complete compatible segments; no lossy second encode."""

from fractions import Fraction

import av


def remux_segments(sources, destination, fps):
    offset = 0
    signature = None
    destination.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(destination), "w") as output:
        stream = None
        for path in sources:
            with av.open(str(path)) as source:
                original = source.streams.video[0]
                codec = original.codec_context
                current = (codec.name, codec.width, codec.height, codec.extradata)
                if signature is not None and current != signature:
                    raise ValueError("incompatible video segments; re-encode required")
                signature = current
                if stream is None:
                    stream = output.add_stream_from_template(original)
                    stream.time_base = Fraction(1, int(fps))
                count = 0
                for packet in source.demux(original):
                    if packet.dts is None:
                        continue
                    # Recorder uses zero-latency H264: one packet/frame, no B frames.
                    if packet.pts != packet.dts:
                        raise ValueError("packet copy requires videos without B frames")
                    packet.pts = packet.dts = offset + count
                    packet.duration = 1
                    packet.time_base = Fraction(1, int(fps))
                    packet.stream = stream
                    output.mux(packet)
                    count += 1
                offset += count
    return offset
