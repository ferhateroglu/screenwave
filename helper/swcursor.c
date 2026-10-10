/* swcursor <node-id>
 *
 * Connects to a Mutter ScreenCast PipeWire node created with
 * cursor-mode=Metadata and prints the real pointer position ("x y\n", in
 * monitor pixels) every time the stream reports it. Video data is ignored.
 */
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>

#include <spa/param/video/format-utils.h>
#include <spa/pod/builder.h>
#include <pipewire/pipewire.h>

#define CURSOR_META_SIZE(w, h) \
    (sizeof(struct spa_meta_cursor) + sizeof(struct spa_meta_bitmap) + (w) * (h) * 4)

struct data {
    struct pw_main_loop *loop;
    struct pw_stream *stream;
};

static void on_process(void *userdata)
{
    struct data *d = userdata;
    struct pw_buffer *b = pw_stream_dequeue_buffer(d->stream);
    if (b == NULL)
        return;
    struct spa_meta_cursor *c =
        spa_buffer_find_meta_data(b->buffer, SPA_META_Cursor, sizeof(*c));
    if (c != NULL && spa_meta_cursor_is_valid(c)) {
        if (printf("%d %d\n", c->position.x, c->position.y) < 0 || fflush(stdout) != 0)
            pw_main_loop_quit(d->loop);
    }
    pw_stream_queue_buffer(d->stream, b);
}

static void on_param_changed(void *userdata, uint32_t id, const struct spa_pod *param)
{
    struct data *d = userdata;
    if (param == NULL || id != SPA_PARAM_Format)
        return;
    uint8_t buf[256];
    struct spa_pod_builder b = SPA_POD_BUILDER_INIT(buf, sizeof(buf));
    const struct spa_pod *params[1];
    params[0] = spa_pod_builder_add_object(&b,
        SPA_TYPE_OBJECT_ParamMeta, SPA_PARAM_Meta,
        SPA_PARAM_META_type, SPA_POD_Id(SPA_META_Cursor),
        SPA_PARAM_META_size, SPA_POD_CHOICE_RANGE_Int(
            (int)CURSOR_META_SIZE(64, 64),
            (int)CURSOR_META_SIZE(1, 1),
            (int)CURSOR_META_SIZE(1024, 1024)));
    pw_stream_update_params(d->stream, params, 1);
}

static void on_state_changed(void *userdata, enum pw_stream_state old,
                             enum pw_stream_state state, const char *error)
{
    struct data *d = userdata;
    (void)old;
    if (state == PW_STREAM_STATE_ERROR) {
        fprintf(stderr, "swcursor: stream error: %s\n", error ? error : "?");
        pw_main_loop_quit(d->loop);
    } else if (state == PW_STREAM_STATE_UNCONNECTED) {
        pw_main_loop_quit(d->loop);
    }
}

static const struct pw_stream_events stream_events = {
    PW_VERSION_STREAM_EVENTS,
    .state_changed = on_state_changed,
    .param_changed = on_param_changed,
    .process = on_process,
};

static void on_signal(void *userdata, int sig)
{
    (void)sig;
    pw_main_loop_quit(((struct data *)userdata)->loop);
}

int main(int argc, char *argv[])
{
    if (argc < 2) {
        fprintf(stderr, "usage: swcursor <node-id>\n");
        return 2;
    }
    uint32_t node = (uint32_t)strtoul(argv[1], NULL, 10);
    struct data d = {0};

    pw_init(&argc, &argv);
    d.loop = pw_main_loop_new(NULL);
    pw_loop_add_signal(pw_main_loop_get_loop(d.loop), SIGINT, on_signal, &d);
    pw_loop_add_signal(pw_main_loop_get_loop(d.loop), SIGTERM, on_signal, &d);

    d.stream = pw_stream_new_simple(pw_main_loop_get_loop(d.loop), "screenwave-cursor",
        pw_properties_new(PW_KEY_MEDIA_TYPE, "Video",
                          PW_KEY_MEDIA_CATEGORY, "Capture",
                          PW_KEY_MEDIA_ROLE, "Screen", NULL),
        &stream_events, &d);

    uint8_t buf[1024];
    struct spa_pod_builder b = SPA_POD_BUILDER_INIT(buf, sizeof(buf));
    struct spa_rectangle def = SPA_RECTANGLE(1920, 1080);
    struct spa_rectangle min = SPA_RECTANGLE(1, 1);
    struct spa_rectangle max = SPA_RECTANGLE(16384, 16384);
    struct spa_fraction fdef = SPA_FRACTION(30, 1);
    struct spa_fraction fmin = SPA_FRACTION(0, 1);
    struct spa_fraction fmax = SPA_FRACTION(120, 1);
    const struct spa_pod *params[1];
    params[0] = spa_pod_builder_add_object(&b,
        SPA_TYPE_OBJECT_Format, SPA_PARAM_EnumFormat,
        SPA_FORMAT_mediaType, SPA_POD_Id(SPA_MEDIA_TYPE_video),
        SPA_FORMAT_mediaSubtype, SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
        SPA_FORMAT_VIDEO_format, SPA_POD_CHOICE_ENUM_Id(5,
            SPA_VIDEO_FORMAT_BGRx, SPA_VIDEO_FORMAT_BGRx, SPA_VIDEO_FORMAT_BGRA,
            SPA_VIDEO_FORMAT_RGBx, SPA_VIDEO_FORMAT_RGBA),
        SPA_FORMAT_VIDEO_size, SPA_POD_CHOICE_RANGE_Rectangle(&def, &min, &max),
        SPA_FORMAT_VIDEO_framerate, SPA_POD_CHOICE_RANGE_Fraction(&fdef, &fmin, &fmax));

    if (pw_stream_connect(d.stream, PW_DIRECTION_INPUT, node,
            PW_STREAM_FLAG_AUTOCONNECT | PW_STREAM_FLAG_MAP_BUFFERS, params, 1) < 0) {
        fprintf(stderr, "swcursor: cannot connect to node %u\n", node);
        return 1;
    }

    pw_main_loop_run(d.loop);
    pw_stream_destroy(d.stream);
    pw_main_loop_destroy(d.loop);
    pw_deinit();
    return 0;
}
