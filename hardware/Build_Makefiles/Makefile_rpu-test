PLUGIN := libgstzoneguard.so
PREFIX ?= /usr
GST_PLUGIN_DIR ?= $(PREFIX)/lib/gstreamer-1.0

# Pick up headers from the system VVAS dev packages.
# -I.. picks up ../shm_alert.h (APU/R5 shared struct, single source of truth).
# -pthread: SCHED_FIFO promotion + affinity pinning on the streaming thread.
CFLAGS := -O2 -Wall -Wextra -fPIC -std=gnu11 -DVVAS_GLIB_UTILS=1 -I.. -pthread \
    $(shell pkg-config --cflags gstreamer-1.0 gstreamer-base-1.0 gstreamer-video-1.0 \
                                 vvas-gst-plugins vvas-utils glib-2.0)
LDFLAGS := $(shell pkg-config --libs gstreamer-1.0 gstreamer-base-1.0 gstreamer-video-1.0 \
                                     vvas-gst-plugins vvas-utils glib-2.0) \
           -lvvascore_common-1.0 -lgstvvasinfermeta-3.0 -ljansson -lm -pthread

all: $(PLUGIN)

$(PLUGIN): zoneguard.c
	$(CC) $(CFLAGS) -shared -o $@ $< $(LDFLAGS)

install: $(PLUGIN)
	install -d $(DESTDIR)$(GST_PLUGIN_DIR)
	install -m 0644 $(PLUGIN) $(DESTDIR)$(GST_PLUGIN_DIR)/$(PLUGIN)

clean:
	rm -f $(PLUGIN)
