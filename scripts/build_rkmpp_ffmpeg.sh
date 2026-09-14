#!/usr/bin/env bash
# Build the pinned RK3588 H.264 runtime. Override the two repository URLs with
# reachable mirrors in mainland China; commits remain fixed and are verified.
set -euo pipefail

MPP_COMMIT=c1ce7e1a612644e6684481afe8045d2bfa680aba
FFMPEG_COMMIT=d90e3a1c18d7929383cf88c1b3da2e2d1c966cbf
MPP_REPO=${YAM_MPP_REPO:-https://github.com/rockchip-linux/mpp.git}
FFMPEG_REPO=${YAM_FFMPEG_ROCKCHIP_REPO:-https://github.com/nyanmisaka/ffmpeg-rockchip.git}
BUILD_ROOT=${YAM_RKMPP_BUILD_ROOT:-build/rkmpp}
INSTALL_PREFIX=/opt/yam-rkmpp
CURRENT_USER=$(id -un)
TARGET_USER=${SUDO_USER:-$CURRENT_USER}

for command in cmake ninja gcc g++ make pkg-config git; do
  command -v "$command" >/dev/null || { echo "missing build command: $command" >&2; exit 1; }
done
pkg-config --exists libdrm || {
  echo "missing libdrm development files; install libdrm-dev" >&2
  exit 1
}

mkdir -p "$BUILD_ROOT/src" "$BUILD_ROOT/mpp-build" "$BUILD_ROOT/prefix" \
  "$BUILD_ROOT/ffmpeg-build" "$BUILD_ROOT/product/bin" "$BUILD_ROOT/product/lib"

fetch_fixed() {
  local repo=$1 destination=$2 commit=$3
  if [ ! -d "$destination/.git" ]; then
    git clone --filter=blob:none --no-checkout "$repo" "$destination"
  fi
  git -C "$destination" fetch --depth 1 origin "$commit"
  git -C "$destination" checkout --detach "$commit"
  test "$(git -C "$destination" rev-parse HEAD)" = "$commit"
}

fetch_fixed "$MPP_REPO" "$BUILD_ROOT/src/mpp" "$MPP_COMMIT"
fetch_fixed "$FFMPEG_REPO" "$BUILD_ROOT/src/ffmpeg-rockchip" "$FFMPEG_COMMIT"

cmake -S "$BUILD_ROOT/src/mpp" -B "$BUILD_ROOT/mpp-build" -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$(realpath "$BUILD_ROOT/prefix")" \
  -DBUILD_SHARED_LIBS=ON -DBUILD_TEST=OFF
cmake --build "$BUILD_ROOT/mpp-build" -j"$(nproc)"
cmake --install "$BUILD_ROOT/mpp-build"

ffmpeg_source=$(realpath "$BUILD_ROOT/src/ffmpeg-rockchip")
prefix=$(realpath "$BUILD_ROOT/prefix")
(
  cd "$BUILD_ROOT/ffmpeg-build"
  PKG_CONFIG_PATH="$prefix/lib/pkgconfig" "$ffmpeg_source/configure" \
    --prefix="$INSTALL_PREFIX" --enable-version3 --enable-libdrm --enable-rkmpp \
    --disable-everything --enable-ffmpeg --disable-ffprobe \
    --enable-encoder=h264_rkmpp --enable-decoder=rawvideo --enable-demuxer=rawvideo \
    --enable-muxer=mp4 --enable-protocol=file --enable-protocol=pipe \
    --extra-ldflags="-Wl,-rpath,$INSTALL_PREFIX/lib"
  make -j"$(nproc)"
)

install -m 0755 "$BUILD_ROOT/ffmpeg-build/ffmpeg" "$BUILD_ROOT/product/bin/ffmpeg"
install -m 0644 "$BUILD_ROOT/prefix/lib/librockchip_mpp.so.0" \
  "$BUILD_ROOT/product/lib/librockchip_mpp.so.0"
ln -sfn librockchip_mpp.so.0 "$BUILD_ROOT/product/lib/librockchip_mpp.so.1"
ln -sfn librockchip_mpp.so.1 "$BUILD_ROOT/product/lib/librockchip_mpp.so"

sudo install -d -m 0755 "$INSTALL_PREFIX/bin" "$INSTALL_PREFIX/lib"
sudo install -m 0755 "$BUILD_ROOT/product/bin/ffmpeg" "$INSTALL_PREFIX/bin/ffmpeg"
sudo install -m 0644 "$BUILD_ROOT/product/lib/librockchip_mpp.so.0" \
  "$INSTALL_PREFIX/lib/librockchip_mpp.so.0"
sudo ln -sfn librockchip_mpp.so.0 "$INSTALL_PREFIX/lib/librockchip_mpp.so.1"
sudo ln -sfn librockchip_mpp.so.1 "$INSTALL_PREFIX/lib/librockchip_mpp.so"
sudo install -m 0644 scripts/92-yam-rkmpp.rules /etc/udev/rules.d/92-yam-rkmpp.rules
sudo usermod -aG video,render "$TARGET_USER"
sudo udevadm control --reload-rules
sudo udevadm trigger --action=add --subsystem-match=mpp_class
sudo udevadm trigger --action=add --subsystem-match=misc
sudo udevadm trigger --action=add --subsystem-match=dma_heap

"$INSTALL_PREFIX/bin/ffmpeg" -hide_banner -encoders 2>/dev/null | grep h264_rkmpp
echo "installed $INSTALL_PREFIX; log out/in once if video/render groups were newly added"
