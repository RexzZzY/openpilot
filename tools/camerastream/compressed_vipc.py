#!/usr/bin/env python3
import os
import sys
import argparse
import numpy as np
import multiprocessing
import time

import cereal.messaging as messaging
from cereal.visionipc.visionipc_pyx import VisionIpcServer, VisionStreamType  # pylint: disable=no-name-in-module, import-error

W, H = 1928, 1208
V4L2_BUF_FLAG_KEYFRAME = 8

def decoder(addr, sock_name, vipc_server, vst, nvidia):
  print("start decoder for %s" % sock_name)
  if nvidia:
    sys.path += os.environ["LD_LIBRARY_PATH"].split(":")
    import PyNvCodec as nvc # pylint: disable=import-error

    nvDec = nvc.PyNvDecoder(W, H, nvc.PixelFormat.NV12, nvc.CudaVideoCodec.HEVC, 0)
    cc1 = nvc.ColorspaceConversionContext(nvc.ColorSpace.BT_709, nvc.ColorRange.JPEG)
    conv_yuv = nvc.PySurfaceConverter(W, H, nvc.PixelFormat.NV12, nvc.PixelFormat.YUV420, 0)
    nvDwn_yuv = nvc.PySurfaceDownloader(W, H, nvc.PixelFormat.YUV420, 0)
    img_yuv = np.ndarray((H*W//2*3), dtype=np.uint8)
  else:
    import av # pylint: disable=import-error
    codec = av.CodecContext.create("hevc", "r")

  os.environ["ZMQ"] = "1"
  messaging.context = messaging.Context()
  sock = messaging.sub_sock(sock_name, None, addr=addr, conflate=False)
  cnt = 0
  last_idx = -1
  seen_iframe = False

  time_q = []
  while 1:
    msgs = messaging.drain_sock(sock, wait_for_one=True)
    for evt in msgs:
      evta = getattr(evt, evt.which())
      if evta.idx.encodeId != 0 and evta.idx.encodeId != (last_idx+1):
        print("DROP PACKET!")
      last_idx = evta.idx.encodeId
      if not seen_iframe and not (evta.idx.flags & V4L2_BUF_FLAG_KEYFRAME):
        print("waiting for iframe")
        continue
      time_q.append(time.monotonic())
      latency = ((evt.logMonoTime/1e9) - (evta.idx.timestampEof/1e9))*1000

      # put in header (first)
      if not seen_iframe:
        if nvidia:
          nvDec.DecodeSurfaceFromPacket(np.frombuffer(evta.header, dtype=np.uint8))
        else:
          codec.decode(av.packet.Packet(evta.header))
        seen_iframe = True

      if nvidia:
        rawSurface = nvDec.DecodeSurfaceFromPacket(np.frombuffer(evta.data, dtype=np.uint8))
        if rawSurface.Empty():
          print("DROP SURFACE")
          continue
        convSurface = conv_yuv.Execute(rawSurface, cc1)
        nvDwn_yuv.DownloadSingleSurface(convSurface, img_yuv)
      else:
        frames = codec.decode(av.packet.Packet(evta.data))
        if len(frames) == 0:
          print("DROP SURFACE")
          continue
        assert len(frames) == 1
        img_yuv = frames[0].to_ndarray(format=av.video.format.VideoFormat('yuv420p'))

      vipc_server.send(vst, img_yuv.flatten().data, cnt, 0, 0)
      cnt += 1

      pc_latency = (time.monotonic()-time_q[0])*1000
      time_q = time_q[1:]
      print("%2d %4d %.3f %.3f latency %6.2fms + %6.2f ms" % (len(msgs), evta.idx.encodeId, evt.logMonoTime/1e9, evta.idx.timestampEof/1e6, latency, pc_latency), len(evta.data), sock_name)

if __name__ == "__main__":
  parser = argparse.ArgumentParser(description='Decode video streams and broacast on VisionIPC')
  parser.add_argument("addr", help="Address of comma 3")
  parser.add_argument('--nvidia', action='store_true', help='Use nvidia instead of ffmpeg')
  parser.add_argument("--cams", default="0,1,2", help="Cameras to decode")
  args = parser.parse_args()

  all_cams = [
    ("roadEncodeData", VisionStreamType.VISION_STREAM_ROAD),
    ("wideRoadEncodeData", VisionStreamType.VISION_STREAM_WIDE_ROAD),
    ("driverEncodeData", VisionStreamType.VISION_STREAM_DRIVER),
  ]
  cams = dict([all_cams[int(x)] for x in args.cams.split(",")])

  vipc_server = VisionIpcServer("camerad")
  for vst in cams.values():
    vipc_server.create_buffers(vst, 4, False, W, H)
  vipc_server.start_listener()

  for k,v in cams.items():
    multiprocessing.Process(target=decoder, args=(args.addr, k, vipc_server, v, args.nvidia)).start()
