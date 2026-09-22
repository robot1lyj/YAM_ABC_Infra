#!/usr/bin/env bash
# Build only: never installs or loads a module.
set -euo pipefail
if [[ $# != 2 ]]; then
    echo "Usage: $0 /path/to/original/gs_usb.c /new/build/directory" >&2
    exit 2
fi
source_file=$(realpath "$1")
output=$(realpath -m "$2")
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
kernel=6.1.118
source_sha=4617d004d03b8cedc03d1bc4d1c7a03b3e890404e40fd69922b0cf9aba076eb5
patched_sha=860ad6bc18f2ea8be8050c4e9981131538ea47d8132b129fc01ce760e6647b10
[[ $(uname -r) == "$kernel" && $(uname -m) == aarch64 ]] || {
    echo "Build on the RK3588 running 6.1.118/aarch64." >&2; exit 1;
}
[[ $(sha256sum "$source_file" | cut -d ' ' -f1) == "$source_sha" ]] || {
    echo "Source differs from the audited Linux v6.1.118 file." >&2; exit 1;
}
[[ ! -e "$output" ]] || { echo "Use a new build directory." >&2; exit 1; }
mkdir -p "$output"
cp -- "$source_file" "$output/gs_usb.c"
patch --batch --fuzz=0 -d "$output" -p1 < "$script_dir/urb-lifecycle.patch"
[[ $(sha256sum "$output/gs_usb.c" | cut -d ' ' -f1) == "$patched_sha" ]]
printf 'obj-m += gs_usb.o\n' > "$output/Makefile"
make -C "/lib/modules/$kernel/build" M="$output" -j2 W=1 modules 2>&1 | tee "$output/build.log"
[[ $(modinfo -F version "$output/gs_usb.ko") == 6.1.118-yam1 ]]
[[ $(modinfo -F vermagic "$output/gs_usb.ko") == "$(modinfo -F vermagic gs_usb)" ]]
(
    cd "$output"
    sha256sum gs_usb.c gs_usb.ko > SHA256SUMS
    modinfo ./gs_usb.ko > modinfo.txt
    uname -a > build-host.txt
    gcc --version >> build-host.txt
)
echo "Built and checked: $output/gs_usb.ko (not installed or loaded)"
