#!/usr/bin/env bash
set -euo pipefail

found=false
for package_dir in packages/*; do
  [[ -f "$package_dir/PKGBUILD" ]] || continue
  found=true
  package_name="$(basename "$package_dir")"
  (
    cd "$package_dir"
    diff .SRCINFO <(makepkg --printsrcinfo)
    makepkg --syncdeps --cleanbuild --noconfirm

    namcap PKGBUILD | tee namcap-pkgbuild.log
    if grep -F ' E: ' namcap-pkgbuild.log; then
      exit 1
    fi

    while IFS= read -r package_file; do
      namcap "$package_file" | tee namcap-package.log
      if grep -F ' E: ' namcap-package.log; then
        exit 1
      fi
      sudo pacman -U --noconfirm "$package_file"
    done < <(makepkg --packagelist)

    if [[ "$package_name" == podkit-bin ]]; then
      source PKGBUILD
      podkit --version | grep -F "$pkgver"
      podkit --help >/dev/null
      pacman -Ql podkit-bin | grep -Fx 'podkit-bin /usr/bin/podkit'
      pacman -Ql podkit-bin | grep -Fx 'podkit-bin /usr/share/licenses/podkit-bin/LICENSE'
    fi
  )
done

if [[ "$found" != true ]]; then
  echo 'No package directories found under packages/.' >&2
  exit 1
fi
