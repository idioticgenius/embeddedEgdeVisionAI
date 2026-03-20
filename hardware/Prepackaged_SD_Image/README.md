# Prepackaged SD Image

The full SD-card image (`edgevision_full.img`, ~15 GB) is too large to commit
to GitHub directly. It is published as a release artifact on the GitHub
releases page once the project is tagged.

To rebuild from source:

    cd ../../software/OS/PetaLinux_BSP
    petalinux-build
    petalinux-package --boot --u-boot --fpga --force
    petalinux-package --wic --bootfiles "BOOT.BIN boot.scr Image system.dtb"

The resulting `images/linux/petalinux-sdimage.wic` is the same image.
