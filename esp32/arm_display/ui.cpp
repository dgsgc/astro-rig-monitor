// LVGL screens: overview (one row per rig) and the rig screen, which has two swipeable pages:
// sky camera and guide graph. Layout per the approved mockups, 480x320 landscape.
// Everything here runs with the display lock held (from loop() or from LVGL's own task for events).
#include <lvgl.h>
#include <JPEGDEC.h>
#include <new>
#include "model.h"

// ---- colours: from the tools' shared design (dgsgc.github.io design/suite.css), as on the web page.
#define C_BG        0x0F1216
#define C_ROW       0x171B21
#define C_ROW_ALERT 0x4A1716
#define C_LINE      0x2A3038
#define C_TEXT      0xE6E9EE
#define C_TEXT2     0xA9B2BF
#define C_MUTED     0x8A94A3
#define C_GREY      0x5C6470
#define C_GREEN     0x4FB06D
#define C_GREEN_LT  0x6FCF87
#define C_AMBER     0xF0A530
#define C_AMBER_LT  0xFAC775
#define C_AMBER_DK  0x854F0B
#define C_RED       0xE5534B
#define C_RED_LT    0xF7C1C1
#define C_RED_MID   0xF09595
#define C_RED_DK    0xA32D2D
#define C_RA        0x4B95E3
#define C_DEC       0xE0693D

#define SCALE 100   // chart values are hundredths of an arcsecond

volatile bool ui_dimmed = false;

static lv_obj_t *scr_overview, *scr_detail;
static lv_obj_t *sb_time, *sb_bridge, *sb_power;

struct RowUi {
    lv_obj_t *row, *name, *line1, *line2, *big, *sub, *chart, *icon;
    lv_chart_series_t *s_total, *s_warn, *s_alert;
};
// The overview's rows are slots: with more rigs than rows, pick_rows() decides which rig each shows.
static const int NROWS = RIG_COUNT < OVERVIEW_ROWS ? RIG_COUNT : OVERVIEW_ROWS;
static RowUi rows[NROWS];
static int slot_rig[NROWS];

static struct {
    lv_obj_t *name, *chip, *guiding_for, *total, *axes, *stats, *chart, *btn_arm, *lbl_arm, *caption;
    lv_obj_t *total_lbl, *sky_total_lbl;
    lv_chart_series_t *s_ra, *s_dec, *s_size, *s_stars, *s_fwarn;   // the last three: trackers
    lv_obj_t *tv, *tile_sky, *tile_graph, *dots, *arm_note;
    lv_obj_t *sky_img, *sky_none, *sky_col, *sky_age, *sky_total, *sky_note, *sky_hint;
} det;

// Sky images decoded to LVGL's 16-bit colour, in PSRAM
struct SkyPic {
    uint16_t *px;
    size_t cap;                   // pixels allocated
    int w, h, rig;
    lv_img_dsc_t dsc;
    lv_obj_t *img;
};
static JPEGDEC *jpeg_dec;
static SkyPic pics[SKY_SIZES] = {{nullptr, 0, 0, 0, -1}, {nullptr, 0, 0, 0, -1}};

// The sky picture on the whole screen: opened and closed with a tap
static struct { lv_obj_t *scr, *img, *none, *caption; } full;
static bool full_open = false;

static lv_color_t col(uint32_t hex) { return lv_color_hex(hex); }

static lv_obj_t *label(lv_obj_t *parent, const lv_font_t *font, uint32_t color, const char *text = "") {
    lv_obj_t *l = lv_label_create(parent);
    lv_obj_set_style_text_font(l, font, 0);
    lv_obj_set_style_text_color(l, col(color), 0);
    lv_label_set_text(l, text);
    return l;
}

static lv_obj_t *plain(lv_obj_t *parent) {
    lv_obj_t *o = lv_obj_create(parent);
    lv_obj_remove_style_all(o);
    lv_obj_clear_flag(o, LV_OBJ_FLAG_SCROLLABLE);
    return o;
}

static lv_obj_t *make_chart(lv_obj_t *parent, int w, int h, int points) {
    lv_obj_t *c = lv_chart_create(parent);
    lv_obj_set_size(c, w, h);
    lv_chart_set_type(c, LV_CHART_TYPE_LINE);
    lv_chart_set_point_count(c, points);
    lv_chart_set_update_mode(c, LV_CHART_UPDATE_MODE_SHIFT);
    lv_obj_set_style_bg_opa(c, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(c, 0, 0);
    lv_obj_set_style_pad_all(c, 0, 0);
    lv_obj_set_style_size(c, 0, LV_PART_INDICATOR);          // no point dots
    lv_obj_set_style_line_width(c, 2, LV_PART_ITEMS);
    lv_obj_clear_flag(c, LV_OBJ_FLAG_CLICKABLE);             // taps go to the row behind
    lv_obj_clear_flag(c, LV_OBJ_FLAG_SCROLLABLE);
    return c;
}

static void fill_series(lv_obj_t *chart, lv_chart_series_t *s, int n, float v) {
    lv_coord_t *y = lv_chart_get_y_array(chart, s);
    for (int i = 0; i < n; i++) y[i] = isnan(v) ? LV_CHART_POINT_NONE : (lv_coord_t)lroundf(v * SCALE);
}

// ---- events (LVGL task) ----

static bool swallow_if_dimmed() { return ui_dimmed; }   // first touch on a dim screen only wakes it
static uint32_t detail_opened_ms = 0;   // the touch that opens a rig screen mustn't also press its buttons
static uint32_t full_opened_ms = 0;     // nor may the tap that opens the full-screen sky close it again
                                        // (the touch controller often reports one tap as two)

static void row_clicked(lv_event_t *e) {
    if (swallow_if_dimmed()) return;
    int rig = slot_rig[(int)(intptr_t)lv_event_get_user_data(e)];
    if (rig < 0) return;
    detail_rig = rig;
    // Sky first when the rig is flagged for something cloud could cause; graph otherwise.
    const RigView &r = rigs[detail_rig];
    Effective ef = effective(detail_rig);
    bool cloudy_reason = !strcmp(r.reason, "rms_high") || !strcmp(r.reason, "star_lost") || !strcmp(r.reason, "stopped") ||
                         !strcmp(r.reason, "cloud");
    bool sky_first = r.has_sky && !ef.stale && !ef.no_bridge && ef.level >= LVL_WARN && cloudy_reason;
    lv_obj_set_tile_id(det.tv, sky_first ? 0 : 1, 0, LV_ANIM_OFF);
    lv_scr_load(scr_detail);
    detail_opened_ms = millis();
    for (int i = 0; i < RIG_COUNT; i++) rigs[i].dirty = true;
}

static void sky_clicked(lv_event_t *) {
    if (swallow_if_dimmed() || detail_rig < 0 || millis() - detail_opened_ms < 800) return;
    full_open = true;
    lv_obj_add_flag(full.img, LV_OBJ_FLAG_HIDDEN);   // drawn from loop() (it decodes), not from this task
    lv_label_set_text(full.none, "");
    lv_label_set_text(full.caption, "");
    rigs[detail_rig].dirty = true;
    lv_scr_load(full.scr);
    full_opened_ms = millis();
}

static void full_clicked(lv_event_t *) {
    if (swallow_if_dimmed() || millis() - full_opened_ms < 800) return;
    full_open = false;
    detail_opened_ms = millis();      // the same tap mustn't press anything behind it
    lv_scr_load(scr_detail);
    if (detail_rig >= 0) rigs[detail_rig].dirty = true;
}

static void back_clicked(lv_event_t *) {
    if (swallow_if_dimmed()) return;
    detail_rig = -1;
    lv_scr_load(scr_overview);
    for (int i = 0; i < RIG_COUNT; i++) rigs[i].dirty = true;
}

static void arm_clicked(lv_event_t *) {
    if (swallow_if_dimmed() || detail_rig < 0 || millis() - detail_opened_ms < 800) return;
    RigView &r = rigs[detail_rig];
    pending_cmd_rig = detail_rig;
    pending_cmd_action = r.armed ? 2 : 3;
    r.armed = !r.armed;
    r.dirty = true;
}

// ---- build ----

static void build_status_bar(lv_obj_t *scr) {
    lv_obj_t *bar = plain(scr);
    lv_obj_set_size(bar, 480, 24);
    lv_obj_set_style_border_side(bar, LV_BORDER_SIDE_BOTTOM, 0);
    lv_obj_set_style_border_width(bar, 1, 0);
    lv_obj_set_style_border_color(bar, col(C_LINE), 0);
    sb_time = label(bar, &lv_font_montserrat_12, C_TEXT2, "--:--");
    lv_obj_align(sb_time, LV_ALIGN_LEFT_MID, 10, 0);
    sb_bridge = label(bar, &lv_font_montserrat_12, C_TEXT2, "Waiting for bridge");
    lv_obj_align(sb_bridge, LV_ALIGN_CENTER, 0, 0);
    sb_power = label(bar, &lv_font_montserrat_12, C_TEXT2, "");
    lv_obj_align(sb_power, LV_ALIGN_RIGHT_MID, -10, 0);
}

static void build_row(lv_obj_t *parent, int i) {
    RowUi &u = rows[i];
    u.row = plain(parent);
    lv_obj_set_width(u.row, lv_pct(100));
    lv_obj_set_flex_grow(u.row, 1);
    lv_obj_set_style_bg_opa(u.row, LV_OPA_COVER, 0);
    lv_obj_set_style_bg_color(u.row, col(C_ROW), 0);
    lv_obj_set_style_border_side(u.row, LV_BORDER_SIDE_LEFT, 0);
    lv_obj_set_style_border_width(u.row, 6, 0);
    lv_obj_set_style_border_color(u.row, col(C_GREY), 0);
    lv_obj_add_flag(u.row, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(u.row, row_clicked, LV_EVENT_SHORT_CLICKED, (void *)(intptr_t)i);

    lv_obj_t *left = plain(u.row);
    lv_obj_set_size(left, 140, lv_pct(100));
    lv_obj_align(left, LV_ALIGN_LEFT_MID, 8, 0);
    lv_obj_set_flex_flow(left, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(left, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
    lv_obj_set_style_pad_row(left, 2, 0);
    u.name = label(left, &lv_font_montserrat_16, C_TEXT, RIGS[i].label);
    slot_rig[i] = i;
    u.line1 = label(left, &lv_font_montserrat_12, C_MUTED, "");
    u.line2 = label(left, &lv_font_montserrat_12, C_MUTED, "");
    lv_label_set_long_mode(u.line1, LV_LABEL_LONG_CLIP);
    lv_label_set_long_mode(u.line2, LV_LABEL_LONG_CLIP);
    lv_obj_set_width(u.line1, 136);
    lv_obj_set_width(u.line2, 136);

    lv_obj_t *mid = plain(u.row);
    lv_obj_set_size(mid, 180, lv_pct(100));
    lv_obj_align(mid, LV_ALIGN_LEFT_MID, 154, 0);
    lv_obj_set_flex_flow(mid, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(mid, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_START);
    u.big = label(mid, &lv_font_montserrat_28, C_TEXT, "--");
    u.sub = label(mid, &lv_font_montserrat_12, C_TEXT2, "");

    u.chart = make_chart(u.row, 110, 52, TREND_POINTS);
    lv_obj_align(u.chart, LV_ALIGN_RIGHT_MID, -8, 0);
    lv_chart_set_div_line_count(u.chart, 0, 0);
    u.s_alert = lv_chart_add_series(u.chart, col(C_RED_DK), LV_CHART_AXIS_PRIMARY_Y);
    u.s_warn = lv_chart_add_series(u.chart, col(C_AMBER_DK), LV_CHART_AXIS_PRIMARY_Y);
    u.s_total = lv_chart_add_series(u.chart, col(C_GREEN_LT), LV_CHART_AXIS_PRIMARY_Y);
    lv_chart_set_all_value(u.chart, u.s_total, LV_CHART_POINT_NONE);

    u.icon = label(u.row, &lv_font_montserrat_28, C_RED_MID, LV_SYMBOL_WARNING);
    lv_obj_align(u.icon, LV_ALIGN_RIGHT_MID, -50, 0);
    lv_obj_add_flag(u.icon, LV_OBJ_FLAG_HIDDEN);
}

static void build_overview() {
    scr_overview = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(scr_overview, col(C_BG), 0);
    lv_obj_clear_flag(scr_overview, LV_OBJ_FLAG_SCROLLABLE);
    build_status_bar(scr_overview);
    lv_obj_t *list = plain(scr_overview);
    lv_obj_set_size(list, 480, 296);
    lv_obj_align(list, LV_ALIGN_TOP_LEFT, 0, 24);
    lv_obj_set_flex_flow(list, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_all(list, 3, 0);
    lv_obj_set_style_pad_row(list, 3, 0);
    for (int i = 0; i < NROWS; i++) build_row(list, i);
}

static lv_obj_t *button(lv_obj_t *parent, const char *text, lv_event_cb_t cb, lv_obj_t **lbl_out = nullptr) {
    lv_obj_t *b = lv_btn_create(parent);
    lv_obj_set_height(b, 40);
    lv_obj_set_flex_grow(b, 1);
    lv_obj_set_style_bg_opa(b, LV_OPA_TRANSP, 0);
    lv_obj_set_style_border_width(b, 1, 0);
    lv_obj_set_style_border_color(b, col(C_GREY), 0);
    lv_obj_set_style_radius(b, 6, 0);
    lv_obj_set_style_shadow_width(b, 0, 0);
    lv_obj_t *l = label(b, &lv_font_montserrat_14, C_TEXT, text);
    lv_obj_center(l);
    lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, nullptr);
    if (lbl_out) *lbl_out = l;
    return b;
}

static void build_detail() {
    scr_detail = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(scr_detail, col(C_BG), 0);
    lv_obj_clear_flag(scr_detail, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *head = plain(scr_detail);
    lv_obj_set_size(head, 480, 36);
    lv_obj_set_style_border_side(head, LV_BORDER_SIDE_BOTTOM, 0);
    lv_obj_set_style_border_width(head, 1, 0);
    lv_obj_set_style_border_color(head, col(C_LINE), 0);
    lv_obj_t *back = plain(head);
    lv_obj_set_size(back, 44, 36);
    lv_obj_add_flag(back, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(back, back_clicked, LV_EVENT_CLICKED, nullptr);
    lv_obj_t *arrow = label(back, &lv_font_montserrat_16, C_TEXT, LV_SYMBOL_LEFT);
    lv_obj_center(arrow);
    det.name = label(head, &lv_font_montserrat_16, C_TEXT, "");
    lv_obj_align(det.name, LV_ALIGN_LEFT_MID, 44, 0);
    det.chip = label(head, &lv_font_montserrat_12, C_GREEN_LT, "");
    lv_obj_set_style_bg_opa(det.chip, LV_OPA_COVER, 0);
    lv_obj_set_style_pad_hor(det.chip, 6, 0);
    lv_obj_set_style_pad_ver(det.chip, 1, 0);
    lv_obj_set_style_radius(det.chip, 4, 0);
    lv_obj_align(det.chip, LV_ALIGN_LEFT_MID, 120, 0);
    det.guiding_for = label(head, &lv_font_montserrat_12, C_TEXT2, "");
    lv_obj_align(det.guiding_for, LV_ALIGN_RIGHT_MID, -10, 0);

    // Two pages between the header and the button bar: sky (0) and guide graph (1)
    det.tv = lv_tileview_create(scr_detail);
    lv_obj_set_size(det.tv, 480, 232);
    lv_obj_align(det.tv, LV_ALIGN_TOP_LEFT, 0, 36);
    lv_obj_set_style_bg_opa(det.tv, LV_OPA_TRANSP, 0);
    lv_obj_set_scrollbar_mode(det.tv, LV_SCROLLBAR_MODE_OFF);
    det.tile_sky = lv_tileview_add_tile(det.tv, 0, 0, LV_DIR_RIGHT);
    det.tile_graph = lv_tileview_add_tile(det.tv, 1, 0, LV_DIR_LEFT);
    lv_obj_add_event_cb(det.tv, [](lv_event_t *) { for (int i = 0; i < RIG_COUNT; i++) rigs[i].dirty = true; },
                        LV_EVENT_VALUE_CHANGED, nullptr);

    // Sky page: image on the left, status column on the right
    det.sky_img = lv_img_create(det.tile_sky);
    lv_obj_align(det.sky_img, LV_ALIGN_LEFT_MID, 4, 0);
    lv_obj_add_flag(det.sky_img, LV_OBJ_FLAG_HIDDEN);
    lv_obj_add_flag(det.sky_img, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(det.sky_img, sky_clicked, LV_EVENT_SHORT_CLICKED, nullptr);
    pics[SKY_SMALL].img = det.sky_img;
    det.sky_none = label(det.tile_sky, &lv_font_montserrat_14, C_MUTED, "");
    lv_obj_set_width(det.sky_none, 300);
    lv_obj_set_style_text_align(det.sky_none, LV_TEXT_ALIGN_CENTER, 0);
    lv_obj_align(det.sky_none, LV_ALIGN_LEFT_MID, 10, 0);
    det.sky_col = plain(det.tile_sky);
    lv_obj_set_size(det.sky_col, 148, 224);
    lv_obj_align(det.sky_col, LV_ALIGN_RIGHT_MID, -4, 0);
    lv_obj_set_flex_flow(det.sky_col, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(det.sky_col, 6, 0);
    label(det.sky_col, &lv_font_montserrat_12, C_MUTED, "Sky camera");
    det.sky_age = label(det.sky_col, &lv_font_montserrat_14, C_TEXT, "");
    lv_label_set_long_mode(det.sky_age, LV_LABEL_LONG_WRAP);
    lv_obj_set_width(det.sky_age, 148);
    det.sky_total_lbl = label(det.sky_col, &lv_font_montserrat_12, C_MUTED, "Total RMS");
    det.sky_total = label(det.sky_col, &lv_font_montserrat_26, C_TEXT, "--");
    det.sky_note = label(det.sky_col, &lv_font_montserrat_12, C_TEXT2, "");
    lv_label_set_long_mode(det.sky_note, LV_LABEL_LONG_WRAP);
    lv_obj_set_width(det.sky_note, 148);
    det.sky_hint = label(det.sky_col, &lv_font_montserrat_12, C_MUTED, "Tap picture to enlarge\nSwipe for graph " LV_SYMBOL_RIGHT);

    lv_obj_t *left = plain(det.tile_graph);
    lv_obj_set_size(left, 150, 232);
    lv_obj_align(left, LV_ALIGN_TOP_LEFT, 0, 0);
    lv_obj_set_style_border_side(left, LV_BORDER_SIDE_RIGHT, 0);
    lv_obj_set_style_border_width(left, 1, 0);
    lv_obj_set_style_border_color(left, col(C_LINE), 0);
    lv_obj_set_flex_flow(left, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_all(left, 10, 0);
    lv_obj_set_style_pad_row(left, 6, 0);
    det.total_lbl = label(left, &lv_font_montserrat_12, C_MUTED, "Total RMS");
    det.total = label(left, &lv_font_montserrat_28, C_TEXT, "--");
    det.axes = label(left, &lv_font_montserrat_14, C_TEXT, "");
    lv_label_set_recolor(det.axes, true);
    det.stats = label(left, &lv_font_montserrat_12, C_TEXT2, "");

    lv_obj_t *right = plain(det.tile_graph);
    lv_obj_set_size(right, 330, 232);
    lv_obj_align(right, LV_ALIGN_TOP_LEFT, 150, 0);
    lv_obj_set_style_pad_all(right, 8, 0);
    det.chart = make_chart(right, 314, 196, HISTORY_POINTS);
    lv_obj_align(det.chart, LV_ALIGN_TOP_LEFT, 0, 0);
    lv_chart_set_div_line_count(det.chart, 3, 0);
    lv_obj_set_style_line_color(det.chart, col(C_LINE), LV_PART_MAIN);
    lv_obj_set_style_line_width(det.chart, 1, LV_PART_ITEMS);
    det.s_ra = lv_chart_add_series(det.chart, col(C_RA), LV_CHART_AXIS_PRIMARY_Y);
    det.s_dec = lv_chart_add_series(det.chart, col(C_DEC), LV_CHART_AXIS_PRIMARY_Y);
    det.s_fwarn = lv_chart_add_series(det.chart, col(C_AMBER_DK), LV_CHART_AXIS_PRIMARY_Y);
    det.s_stars = lv_chart_add_series(det.chart, col(C_DEC), LV_CHART_AXIS_SECONDARY_Y);
    det.s_size = lv_chart_add_series(det.chart, col(C_RA), LV_CHART_AXIS_PRIMARY_Y);
    for (lv_chart_series_t *sr : {det.s_fwarn, det.s_stars, det.s_size}) lv_chart_set_all_value(det.chart, sr, LV_CHART_POINT_NONE);
    det.caption = label(right, &lv_font_montserrat_12, C_MUTED, "");
    lv_obj_align(det.caption, LV_ALIGN_BOTTOM_LEFT, 0, 0);

    lv_obj_t *btns = plain(scr_detail);
    lv_obj_set_size(btns, 480, 52);
    lv_obj_align(btns, LV_ALIGN_BOTTOM_LEFT, 0, 0);
    lv_obj_set_style_border_side(btns, LV_BORDER_SIDE_TOP, 0);
    lv_obj_set_style_border_width(btns, 1, 0);
    lv_obj_set_style_border_color(btns, col(C_LINE), 0);
    lv_obj_set_flex_flow(btns, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(btns, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_hor(btns, 10, 0);
    lv_obj_set_style_pad_column(btns, 8, 0);
    det.btn_arm = button(btns, "Disarm", arm_clicked, &det.lbl_arm);
    lv_obj_set_flex_grow(det.btn_arm, 0);
    lv_obj_set_size(det.btn_arm, 110, 34);
    lv_obj_set_style_bg_opa(det.btn_arm, LV_OPA_TRANSP, LV_STATE_DISABLED);
    lv_obj_set_style_border_color(det.btn_arm, col(C_LINE), LV_STATE_DISABLED);
    det.arm_note = label(btns, &lv_font_montserrat_12, C_MUTED, "");
    lv_obj_set_flex_grow(det.arm_note, 1);
    det.dots = label(btns, &lv_font_montserrat_16, C_TEXT2, "");
    lv_obj_set_width(det.dots, 44);
    lv_obj_set_style_text_align(det.dots, LV_TEXT_ALIGN_CENTER, 0);
}

static void build_full() {
    full.scr = lv_obj_create(NULL);
    lv_obj_set_style_bg_color(full.scr, lv_color_black(), 0);
    lv_obj_clear_flag(full.scr, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_add_flag(full.scr, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(full.scr, full_clicked, LV_EVENT_SHORT_CLICKED, nullptr);
    full.img = pics[SKY_FULL].img = lv_img_create(full.scr);
    lv_obj_center(full.img);
    lv_obj_add_flag(full.img, LV_OBJ_FLAG_HIDDEN);
    full.none = label(full.scr, &lv_font_montserrat_14, C_MUTED, "");
    lv_obj_center(full.none);
    full.caption = label(full.scr, &lv_font_montserrat_12, C_TEXT, "");
    lv_obj_set_style_bg_color(full.caption, lv_color_black(), 0);
    lv_obj_set_style_bg_opa(full.caption, LV_OPA_60, 0);
    lv_obj_set_style_pad_hor(full.caption, 6, 0);
    lv_obj_set_style_pad_ver(full.caption, 2, 0);
    lv_obj_align(full.caption, LV_ALIGN_BOTTOM_LEFT, 0, 0);
}

void ui_setup() {
    build_overview();
    build_detail();
    build_full();
    lv_scr_load(scr_overview);
}

// ---- update ----

static const char *state_text(const RigView &r) {
    const char *s = r.state;
    if (!strcmp(s, "guiding")) return "Guiding";
    if (!strcmp(s, "dithering")) return "Dithering";
    if (!strcmp(s, "settling")) return "Settling";
    if (!strcmp(s, "star_lost")) return "Star lost";
    if (!strcmp(s, "stopped")) return "Stopped";
    if (!strcmp(s, "paused")) return "Paused";
    if (!strcmp(s, "looping")) return "Looping";
    if (!strcmp(s, "busy")) return r.task[0] ? (strcmp(r.task, "calibration") ? r.task : "Calibrating") : "Busy";
    if (!strcmp(s, "idle")) return "Not imaging";
    if (!strcmp(s, "unknown")) return "No frames";
    if (r.tracker) {
        if (r.state_text[0] && strcmp(s, "idle")) return r.state_text;
        if (!strcmp(s, "idle")) return "No run";
        if (!strcmp(s, "imaging")) return "Imaging";
        if (!strcmp(s, "focusing")) return "Refocusing";
        if (!strcmp(s, "done")) return "Run done";
        if (!strcmp(s, "error")) return "Error";
    }
    return s[0] ? s : "No data";
}

static void level_colors(Level lv, uint32_t &stripe, uint32_t &accent) {
    switch (lv) {
        case LVL_OK:    stripe = C_GREEN; accent = C_GREEN_LT; break;
        case LVL_WARN:  stripe = C_AMBER; accent = C_AMBER_LT; break;
        case LVL_ALERT: stripe = C_RED;   accent = C_RED_LT;   break;
        default:        stripe = C_GREY;  accent = C_MUTED;    break;
    }
}

static void fmt_arcsec(char *buf, size_t n, float v) {
    if (isnan(v)) snprintf(buf, n, "--");
    else snprintf(buf, n, "%.2f\"", v);
}

static bool tracker_live(const RigView &r, const Effective &e) {
    return r.tracker && !e.stale && !e.no_bridge && r.frames_total > 0 &&
           (!strcmp(r.state, "imaging") || !strcmp(r.state, "dithering") || !strcmp(r.state, "settling") ||
            !strcmp(r.state, "focusing") || !strcmp(r.state, "paused"));
}

static void fmt_frames(char *buf, size_t n, const RigView &r) {
    if (r.frame >= 0 && r.frames_total > 0) snprintf(buf, n, "%d / %d", r.frame, r.frames_total);
    else snprintf(buf, n, "--");
}

static void fmt_stars(char *buf, size_t n, const RigView &r) {
    char sz[12] = "--", ct[12] = "--";
    if (!isnan(r.star_size)) snprintf(sz, sizeof sz, "%.0f\"", r.star_size);
    if (r.stars >= 0) snprintf(ct, sizeof ct, "%d", r.stars);
    snprintf(buf, n, "Stars %s  %s found", sz, ct);
}

static bool data_live(const RigView &r, const Effective &e) {
    return r.has_screen && !e.stale && !e.no_bridge &&
           (!strcmp(r.state, "guiding") || !strcmp(r.state, "dithering") || !strcmp(r.state, "settling"));
}

static void update_tracker_row(int i, RowUi &u, const Effective &e, uint32_t accent);

static void update_row(int slot) {
    int i = slot_rig[slot];
    if (i < 0) return;
    RigView &r = rigs[i];
    RowUi &u = rows[slot];
    Effective e = effective(i);
    uint32_t stripe, accent;
    level_colors(e.level, stripe, accent);
    bool alert = e.level == LVL_ALERT;
    char a[48], b[48], t[16];
    long now = now_unix();

    lv_obj_set_style_border_color(u.row, col(stripe), 0);
    lv_obj_set_style_bg_color(u.row, col(alert ? C_ROW_ALERT : C_ROW), 0);
    lv_label_set_text(u.name, r.name[0] ? r.name : RIGS[i].label);
    lv_obj_set_style_text_color(u.name, col(e.level == LVL_IDLE ? C_TEXT2 : C_TEXT), 0);

    // Line 1: what's happening
    if (e.no_bridge) snprintf(a, sizeof a, "No bridge");
    else if (e.stale) snprintf(a, sizeof a, "No data");
    else if (alert && r.reason_text[0])
        snprintf(a, sizeof a, "%s  %s", r.reason_text, now && r.since ? fmt_elapsed(now - r.since, t, sizeof t) : "");
    else if (e.level == LVL_WARN) snprintf(a, sizeof a, "%s  warning", state_text(r));
    else snprintf(a, sizeof a, "%s", r.have ? state_text(r) : "No data yet");
    if (r.tracker && e.level == LVL_WARN && r.reason_text[0] && !e.stale && !e.no_bridge)
        snprintf(a, sizeof a, "%s", r.reason_text);          // the cause matters more than "warning"
    lv_label_set_text(u.line1, a);
    lv_obj_set_style_text_color(u.line1, col(e.level == LVL_IDLE ? C_MUTED : accent), 0);
    if (r.tracker) { update_tracker_row(i, u, e, accent); return; }

    // Line 2: detail or what to do
    if (alert) snprintf(b, sizeof b, "Tap to disarm");
    else if (!r.armed && r.have) snprintf(b, sizeof b, "Alerts off");
    else if (data_live(r, e)) {
        char snr[16] = "";
        if (!isnan(r.snr)) snprintf(snr, sizeof snr, "SNR %.0f%s  ", r.snr, strcmp(r.snr_trend, "falling") ? "" : " falling");
        long age = (now && r.last_frame_ts) ? now - r.last_frame_ts : -1;
        snprintf(b, sizeof b, "%s%s ago", snr, age >= 0 ? fmt_elapsed(age, t, sizeof t) : "?");
    } else b[0] = 0;
    lv_label_set_text(u.line2, b);
    lv_obj_set_style_text_color(u.line2, col(alert ? C_RED_MID : C_MUTED), 0);

    // Middle: big figure (ASIAIR-matching screen total) and RA/Dec, or last good value
    if (data_live(r, e) && strcmp(r.state, "star_lost")) {
        fmt_arcsec(a, sizeof a, r.total);
        lv_label_set_text(u.big, a);
        lv_obj_set_style_text_color(u.big, col(e.level == LVL_WARN ? C_AMBER_LT : alert ? C_RED_LT : C_TEXT), 0);
        snprintf(b, sizeof b, "RA %.2f  Dec %.2f", r.ra, r.dec);
        lv_label_set_text(u.sub, b);
        lv_obj_set_style_text_color(u.sub, col(C_TEXT2), 0);
    } else {
        lv_label_set_text(u.big, "--");
        lv_obj_set_style_text_color(u.big, col(alert ? C_RED_LT : C_GREY), 0);
        if (!isnan(r.last_good) && r.armed) snprintf(b, sizeof b, "Last good %.2f\"", r.last_good);
        else snprintf(b, sizeof b, "No guiding data");
        lv_label_set_text(u.sub, b);
        lv_obj_set_style_text_color(u.sub, col(alert ? C_RED_MID : C_GREY), 0);
    }

    // Right: sparkline when there's something to show, warning icon on alerts
    bool show_icon = alert && !data_live(r, e);
    if (show_icon) {
        lv_obj_add_flag(u.chart, LV_OBJ_FLAG_HIDDEN);
        lv_obj_clear_flag(u.icon, LV_OBJ_FLAG_HIDDEN);
    } else {
        lv_obj_add_flag(u.icon, LV_OBJ_FLAG_HIDDEN);
        lv_obj_clear_flag(u.chart, LV_OBJ_FLAG_HIDDEN);
        float top = r.alert * 1.3f;
        for (int k = 0; k < TREND_POINTS; k++)
            if (!isnan(r.trend[k]) && r.trend[k] > top) top = r.trend[k];
        lv_chart_set_range(u.chart, LV_CHART_AXIS_PRIMARY_Y, 0, (lv_coord_t)lroundf(top * SCALE));
        fill_series(u.chart, u.s_warn, TREND_POINTS, r.warn);
        fill_series(u.chart, u.s_alert, TREND_POINTS, r.alert);
        lv_chart_set_series_color(u.chart, u.s_total, col(e.level == LVL_IDLE ? C_GREY : accent == C_RED_LT ? C_RED : accent));
        lv_coord_t *y = lv_chart_get_y_array(u.chart, u.s_total);
        for (int k = 0; k < TREND_POINTS; k++)
            y[k] = isnan(r.trend[k]) ? LV_CHART_POINT_NONE : (lv_coord_t)lroundf(r.trend[k] * SCALE);
        lv_chart_refresh(u.chart);
    }
}

// Tracker rows: frame count where guided rigs show RMS, star size and count below it,
// and a star-size trend against the size that warns of focus drift.
static void update_tracker_row(int i, RowUi &u, const Effective &e, uint32_t accent) {
    RigView &r = rigs[i];
    bool alert = e.level == LVL_ALERT, live = tracker_live(r, e);
    char a[48], t[16];
    if (alert) snprintf(a, sizeof a, "Tap to disarm");
    else if (!r.armed && r.have) snprintf(a, sizeof a, "Alerts off");
    else if (live && r.panel > 0)
        snprintf(a, sizeof a, "Panel %d of %d  %s left", r.panel, r.panels, r.remaining_s >= 0 ? fmt_elapsed(r.remaining_s, t, sizeof t) : "?");
    else if (live && r.remaining_s >= 0) snprintf(a, sizeof a, "%s left", fmt_elapsed(r.remaining_s, t, sizeof t));
    else a[0] = 0;
    lv_label_set_text(u.line2, a);
    lv_obj_set_style_text_color(u.line2, col(alert ? C_RED_MID : C_MUTED), 0);

    fmt_frames(a, sizeof a, r);
    lv_label_set_text(u.big, a);
    lv_obj_set_style_text_color(u.big, col(!live ? (alert ? C_RED_LT : C_GREY) : e.level == LVL_WARN ? C_AMBER_LT
                                           : alert ? C_RED_LT : C_TEXT), 0);
    if (live || r.stars >= 0) fmt_stars(a, sizeof a, r);
    else snprintf(a, sizeof a, "No frames measured");
    lv_label_set_text(u.sub, a);
    lv_obj_set_style_text_color(u.sub, col(live ? C_TEXT2 : alert ? C_RED_MID : C_GREY), 0);

    bool show_icon = alert && !live;
    show_icon ? lv_obj_add_flag(u.chart, LV_OBJ_FLAG_HIDDEN) : lv_obj_clear_flag(u.chart, LV_OBJ_FLAG_HIDDEN);
    show_icon ? lv_obj_clear_flag(u.icon, LV_OBJ_FLAG_HIDDEN) : lv_obj_add_flag(u.icon, LV_OBJ_FLAG_HIDDEN);
    if (show_icon) return;
    float warn = isnan(r.base_size) ? NAN : r.base_size * (1 + r.focus_warn_pct / 100);
    float top = isnan(warn) ? 1 : warn * 1.3f;
    for (int k = 0; k < TREND_POINTS; k++)
        if (!isnan(r.trend[k]) && r.trend[k] > top) top = r.trend[k] * 1.1f;
    lv_chart_set_range(u.chart, LV_CHART_AXIS_PRIMARY_Y, 0, (lv_coord_t)lroundf(top * SCALE));
    fill_series(u.chart, u.s_warn, TREND_POINTS, warn);
    fill_series(u.chart, u.s_alert, TREND_POINTS, NAN);
    lv_chart_set_series_color(u.chart, u.s_total, col(e.level == LVL_IDLE ? C_GREY : accent == C_RED_LT ? C_RED : accent));
    lv_coord_t *y = lv_chart_get_y_array(u.chart, u.s_total);
    for (int k = 0; k < TREND_POINTS; k++)
        y[k] = isnan(r.trend[k]) ? LV_CHART_POINT_NONE : (lv_coord_t)lroundf(r.trend[k] * SCALE);
    lv_chart_refresh(u.chart);
}

// Tracker detail page: frames, plan and camera on the left; star size and stars found per measured frame.
static void update_tracker_detail(int i, const Effective &e) {
    RigView &r = rigs[i];
    char a[160], t[16];
    lv_label_set_text(det.total_lbl, "Frame");
    fmt_frames(a, sizeof a, r);
    lv_label_set_text(det.total, a);
    lv_obj_set_style_text_color(det.total, col(e.level == LVL_WARN ? C_AMBER_LT : e.level == LVL_ALERT ? C_RED_LT : C_TEXT), 0);
    if (r.panel > 0) snprintf(a, sizeof a, "Panel %d of %d\n#B4B2A9 %s#", r.panel, r.panels, r.plan);
    else snprintf(a, sizeof a, "%s", r.plan[0] ? r.plan : "Single target");
    lv_label_set_text(det.axes, a);
    char left[24] = "--", cam[32] = "Camera --", start[16] = "";
    if (r.remaining_s >= 0 && r.armed) snprintf(left, sizeof left, "%s", fmt_elapsed(r.remaining_s, t, sizeof t));
    if (r.battery >= 0 || r.shots >= 0) {
        char b[8] = "--", sh[12] = "--";
        if (r.battery >= 0) snprintf(b, sizeof b, "%d%%", r.battery);
        if (r.shots >= 0) snprintf(sh, sizeof sh, "%d", r.shots);
        snprintf(cam, sizeof cam, "Camera %s  %s shots", b, sh);
    }
    if (!isnan(r.base_size)) snprintf(start, sizeof start, "  (start %.0f\")", r.base_size);
    char sz[12] = "--";
    if (!isnan(r.star_size)) snprintf(sz, sizeof sz, "%.0f\"", r.star_size);
    snprintf(a, sizeof a, "Left %s\nDithers %d\n%s\nStars %s%s\nFound %d  refocused %dx",
             left, r.dithers, cam, sz, start, r.stars < 0 ? 0 : r.stars, r.refocused);
    lv_label_set_text(det.stats, a);

    float lo = 1e9f, hi = 0, chi = 10;
    for (int k = 0; k < r.h_count; k++) {
        if (!isnan(r.h_ra[k])) { lo = min(lo, r.h_ra[k]); hi = max(hi, r.h_ra[k]); }
        if (!isnan(r.h_dec[k])) chi = max(chi, r.h_dec[k]);
    }
    float warn = isnan(r.base_size) ? NAN : r.base_size * (1 + r.focus_warn_pct / 100);
    if (!isnan(warn)) hi = max(hi, warn);
    if (lo > hi) { lo = 0; hi = 60; }
    lo = floorf(lo * 0.8f); hi = ceilf(hi * 1.1f);
    lv_chart_set_range(det.chart, LV_CHART_AXIS_PRIMARY_Y, (lv_coord_t)(lo * SCALE), (lv_coord_t)(hi * SCALE));
    lv_chart_set_range(det.chart, LV_CHART_AXIS_SECONDARY_Y, 0, (lv_coord_t)lroundf(chi * 1.2f));
    lv_chart_set_all_value(det.chart, det.s_ra, LV_CHART_POINT_NONE);
    lv_chart_set_all_value(det.chart, det.s_dec, LV_CHART_POINT_NONE);
    lv_coord_t *ys = lv_chart_get_y_array(det.chart, det.s_size);
    lv_coord_t *yc = lv_chart_get_y_array(det.chart, det.s_stars);
    lv_coord_t *yw = lv_chart_get_y_array(det.chart, det.s_fwarn);
    for (int k = 0; k < HISTORY_POINTS; k++) {     // left-aligned: the run fills the graph as it goes
        float vs = k < r.h_count ? r.h_ra[k] : NAN, vc = k < r.h_count ? r.h_dec[k] : NAN;
        ys[k] = isnan(vs) ? LV_CHART_POINT_NONE : (lv_coord_t)lroundf(vs * SCALE);
        yc[k] = isnan(vc) ? LV_CHART_POINT_NONE : (lv_coord_t)lroundf(vc);
        yw[k] = isnan(warn) ? LV_CHART_POINT_NONE : (lv_coord_t)lroundf(warn * SCALE);
    }
    lv_chart_refresh(det.chart);
    snprintf(a, sizeof a, "%d frames  %.0f-%.0f\"  size blue, stars orange", r.h_count, lo, hi);
    lv_label_set_text(det.caption, a);
}

static void update_sky(int i);

static void update_detail(int i) {
    RigView &r = rigs[i];
    Effective e = effective(i);
    uint32_t stripe, accent;
    level_colors(e.level, stripe, accent);
    char a[96], t[16];
    long now = now_unix();

    lv_label_set_text(det.name, r.name[0] ? r.name : RIGS[i].label);
    static const char *lvl_names[] = {"Idle", "OK", "Warning", "Alert"};
    if (e.no_bridge) snprintf(a, sizeof a, "No bridge");
    else if (e.stale) snprintf(a, sizeof a, "No data");
    else snprintf(a, sizeof a, "%s  %s", state_text(r), lvl_names[e.level]);
    lv_label_set_text(det.chip, a);
    lv_obj_set_style_bg_color(det.chip, col(stripe), 0);
    lv_obj_set_style_text_color(det.chip, col(e.level == LVL_IDLE ? C_TEXT : C_BG), 0);
    lv_obj_align_to(det.chip, det.name, LV_ALIGN_OUT_RIGHT_MID, 10, 0);
    if (now && r.guiding_since && r.armed) {
        snprintf(a, sizeof a, "%s %s", r.tracker ? "running" : "guiding", fmt_elapsed(now - r.guiding_since, t, sizeof t));
        lv_label_set_text(det.guiding_for, a);
    } else lv_label_set_text(det.guiding_for, "");

    if (r.tracker) update_tracker_detail(i, e);
    else {
    lv_label_set_text(det.total_lbl, "Total RMS");
    for (lv_chart_series_t *sr : {det.s_fwarn, det.s_stars, det.s_size}) lv_chart_set_all_value(det.chart, sr, LV_CHART_POINT_NONE);
    fmt_arcsec(a, sizeof a, r.total);
    lv_label_set_text(det.total, a);
    lv_obj_set_style_text_color(det.total, col(e.level == LVL_WARN ? C_AMBER_LT : e.level == LVL_ALERT ? C_RED_LT : C_TEXT), 0);
    char ra[12], dec[12], pk[12];
    fmt_arcsec(ra, sizeof ra, r.ra);
    fmt_arcsec(dec, sizeof dec, r.dec);
    fmt_arcsec(pk, sizeof pk, r.peak);
    snprintf(a, sizeof a, "#85B7EB RA# %s\n#F0997B Dec# %s\n#B4B2A9 Peak# %s", ra, dec, pk);
    lv_label_set_text(det.axes, a);
    char snr[12] = "--", hfd[12] = "--", at[12];
    if (!isnan(r.snr)) snprintf(snr, sizeof snr, "%.0f", r.snr);
    if (!isnan(r.hfd)) snprintf(hfd, sizeof hfd, "%.1f px", r.hfd);
    fmt_arcsec(at, sizeof at, r.alert_total);
    snprintf(a, sizeof a, "SNR %s %s\nHFD %s\nAlert figure %s\nDithers %d  fails %d",
             snr, r.snr_trend, hfd, at, r.dithers, r.settle_fails);
    lv_label_set_text(det.stats, a);

    // Guide graph: RA/Dec, symmetric range, at least +-1"
    float lim = 1.0f;
    for (int k = 0; k < r.h_count; k++) {
        if (!isnan(r.h_ra[k]) && fabsf(r.h_ra[k]) > lim) lim = fabsf(r.h_ra[k]);
        if (!isnan(r.h_dec[k]) && fabsf(r.h_dec[k]) > lim) lim = fabsf(r.h_dec[k]);
    }
    lim = ceilf(lim);
    lv_chart_set_range(det.chart, LV_CHART_AXIS_PRIMARY_Y, (lv_coord_t)(-lim * SCALE), (lv_coord_t)(lim * SCALE));
    lv_coord_t *yr = lv_chart_get_y_array(det.chart, det.s_ra);
    lv_coord_t *yd = lv_chart_get_y_array(det.chart, det.s_dec);
    int pad = HISTORY_POINTS - r.h_count;      // right-align, newest at the right edge
    for (int k = 0; k < HISTORY_POINTS; k++) {
        int src = k - pad;
        float vr = src >= 0 ? r.h_ra[src] : NAN, vd = src >= 0 ? r.h_dec[src] : NAN;
        yr[k] = isnan(vr) ? LV_CHART_POINT_NONE : (lv_coord_t)lroundf(vr * SCALE);
        yd[k] = isnan(vd) ? LV_CHART_POINT_NONE : (lv_coord_t)lroundf(vd * SCALE);
    }
    lv_chart_refresh(det.chart);
    snprintf(a, sizeof a, "Last %d frames  +-%.0f\"  RA blue, Dec orange", r.h_count, lim);
    lv_label_set_text(det.caption, a);
    }

    // Disarm only when something is wrong (so a stray tap can't switch alerts off); Arm always.
    bool problem = e.level >= LVL_WARN || e.stale || e.no_bridge;
    bool can_tap = !r.armed || problem;
    lv_label_set_text(det.lbl_arm, r.armed ? LV_SYMBOL_POWER "  Disarm" : LV_SYMBOL_OK "  Arm");
    if (can_tap) lv_obj_clear_state(det.btn_arm, LV_STATE_DISABLED);
    else lv_obj_add_state(det.btn_arm, LV_STATE_DISABLED);
    lv_obj_set_style_text_color(det.lbl_arm, col(can_tap ? C_TEXT : C_GREY), 0);
    lv_obj_set_style_border_color(det.btn_arm, col(!can_tap ? C_LINE : r.armed ? C_RED : C_GREEN), 0);
    lv_label_set_text(det.arm_note, !r.armed ? (r.tracker ? "Alerts off until the next run" : "Alerts off until guiding starts again")
                                    : problem ? "Disarm stops alerts until the next start"
                                    : "Alerts on");

    update_sky(i);
    bool on_sky = lv_obj_get_scroll_x(det.tv) < 240;
    lv_label_set_text(det.dots, on_sky ? LV_SYMBOL_BULLET " -" : "- " LV_SYMBOL_BULLET);
}

// ---- sky image ----

static SkyPic *decoding;

static int sky_draw(JPEGDRAW *d) {
    SkyPic &p = *decoding;
    for (int row = 0; row < d->iHeight && d->y + row < p.h; row++)
        memcpy(p.px + (d->y + row) * p.w + d->x, d->pPixels + row * d->iWidth,
               sizeof(uint16_t) * min(d->iWidthUsed, p.w - d->x));
    return 1;
}

static void decode_sky(int size) {
    static const int max_w[] = {330, 480}, max_h[] = {232, 320};
    SkyJpeg &j = sky_jpeg[size];
    SkyPic &p = pics[size];
    j.fresh = false;
    if (!jpeg_dec) jpeg_dec = new (ps_malloc(sizeof(JPEGDEC))) JPEGDEC();   // large decoder state: PSRAM
    if (!jpeg_dec->openRAM(j.data, (int)j.len, sky_draw)) return;
    int w = jpeg_dec->getWidth(), h = jpeg_dec->getHeight();
    if (w <= 0 || h <= 0 || w > max_w[size] || h > max_h[size]) { jpeg_dec->close(); return; }
    p.rig = -1;                   // until it has decoded
    if ((size_t)(w * h) > p.cap) {
        free(p.px);
        p.px = (uint16_t *)ps_malloc(sizeof(uint16_t) * w * h);
        p.cap = p.px ? w * h : 0;
        if (!p.px) { p.w = p.h = 0; jpeg_dec->close(); return; }
    }
    p.w = w;
    p.h = h;
    decoding = &p;
    jpeg_dec->setPixelType(LV_COLOR_16_SWAP ? RGB565_BIG_ENDIAN : RGB565_LITTLE_ENDIAN);
    bool ok = jpeg_dec->decode(0, 0, 0);
    jpeg_dec->close();
    if (!ok) return;
    p.rig = j.rig;
    p.dsc.header.always_zero = 0;
    p.dsc.header.cf = LV_IMG_CF_TRUE_COLOR;
    p.dsc.header.w = w;
    p.dsc.header.h = h;
    p.dsc.data_size = sizeof(uint16_t) * w * h;
    p.dsc.data = (const uint8_t *)p.px;
    lv_img_cache_invalidate_src(&p.dsc);
    lv_img_set_src(p.img, &p.dsc);
    lv_obj_invalidate(p.img);
}

static void show(lv_obj_t *o, bool on) {
    if (on) lv_obj_clear_flag(o, LV_OBJ_FLAG_HIDDEN);
    else lv_obj_add_flag(o, LV_OBJ_FLAG_HIDDEN);
}

static void update_full(int i) {
    RigView &r = rigs[i];
    Effective e = effective(i);
    uint32_t stripe, accent;
    level_colors(e.level, stripe, accent);
    char a[96], t[16];
    long now = now_unix();
    if (sky_jpeg[SKY_FULL].fresh && sky_jpeg[SKY_FULL].rig == i) decode_sky(SKY_FULL);
    bool have = pics[SKY_FULL].rig == i;
    show(full.img, have);
    show(full.none, !have);
    if (have) lv_obj_center(full.img);
    else lv_label_set_text(full.none, "Loading sky image...");
    lv_obj_center(full.none);
    // Name, guiding and picture age, in the level's colour
    const char *what = e.no_bridge ? "No bridge" : e.stale ? "No data"
                     : e.level == LVL_ALERT && r.reason_text[0] ? r.reason_text : state_text(r);
    int n = snprintf(a, sizeof a, "%s  %s", r.name[0] ? r.name : RIGS[i].label, what);
    if (data_live(r, e) && n < (int)sizeof a) n += snprintf(a + n, sizeof a - n, "  %.2f\"", r.total);
    if (tracker_live(r, e) && n < (int)sizeof a) n += snprintf(a + n, sizeof a - n, "  frame %d/%d  %d stars", r.frame, r.frames_total, r.stars);
    long ts = r.sky_image_ts ? r.sky_image_ts : r.sky_changed_ts;
    if (!r.sky_ok && n < (int)sizeof a) snprintf(a + n, sizeof a - n, "   can't fetch image");
    else if (ts && now && n < (int)sizeof a) snprintf(a + n, sizeof a - n, "   %s old", fmt_elapsed(max(0L, now - ts), t, sizeof t));
    lv_label_set_text(full.caption, a);
    lv_obj_set_style_text_color(full.caption, col(e.level >= LVL_WARN ? accent : C_TEXT), 0);
    lv_obj_align(full.caption, LV_ALIGN_BOTTOM_LEFT, 0, 0);
}

static void update_sky(int i) {
    RigView &r = rigs[i];
    Effective e = effective(i);
    char a[96], t[16];
    long now = now_unix();
    if (sky_jpeg[SKY_SMALL].fresh && sky_jpeg[SKY_SMALL].rig == i) decode_sky(SKY_SMALL);
    bool have = r.has_sky && pics[SKY_SMALL].rig == i;

    if (have) {
        lv_obj_clear_flag(det.sky_img, LV_OBJ_FLAG_HIDDEN);
        lv_obj_add_flag(det.sky_none, LV_OBJ_FLAG_HIDDEN);
        lv_obj_align(det.sky_img, LV_ALIGN_LEFT_MID, 4 + (320 - pics[SKY_SMALL].w) / 2, 0);
    } else {
        lv_obj_add_flag(det.sky_img, LV_OBJ_FLAG_HIDDEN);
        lv_obj_clear_flag(det.sky_none, LV_OBJ_FLAG_HIDDEN);
        lv_label_set_text(det.sky_none, r.has_sky ? "Loading sky image..." : "No sky camera set up\nfor this rig");
    }

    // How old the picture is: the camera's own time if known, else when the bridge saw it change
    long ts = r.sky_image_ts ? r.sky_image_ts : r.sky_changed_ts;
    uint32_t age_col = C_TEXT;
    if (!r.has_sky) a[0] = 0;
    else if (!r.sky_ok) { snprintf(a, sizeof a, "Can't fetch image:\n%s", r.sky_error); age_col = C_RED_MID; }
    else if (!ts || !now) snprintf(a, sizeof a, "Image age unknown");
    else {
        long age = now - ts;
        if (age < 0) age = 0;
        snprintf(a, sizeof a, "%s old%s", fmt_elapsed(age, t, sizeof t), age > 900 ? "\nCamera may be off" : "");
        if (age > 900) age_col = C_AMBER_LT;
    }
    lv_label_set_text(det.sky_age, a);
    lv_obj_set_style_text_color(det.sky_age, col(age_col), 0);

    bool live = r.tracker ? tracker_live(r, e) : data_live(r, e) && strcmp(r.state, "star_lost");
    lv_label_set_text(det.sky_total_lbl, r.tracker ? "Frame" : "Total RMS");
    if (r.tracker) fmt_frames(a, sizeof a, r);
    else fmt_arcsec(a, sizeof a, live ? r.total : NAN);
    lv_label_set_text(det.sky_total, a);
    lv_obj_set_style_text_color(det.sky_total, col(!live ? C_GREY : e.level == LVL_WARN ? C_AMBER_LT
                                                    : e.level == LVL_ALERT ? C_RED_LT : C_TEXT), 0);
    char note[96] = "";
    if (e.level == LVL_ALERT && r.reason_text[0]) snprintf(note, sizeof note, "%s", r.reason_text);
    else snprintf(note, sizeof note, "%s", state_text(r));
    if (!live && !isnan(r.last_good) && r.armed)
        snprintf(note + strlen(note), sizeof note - strlen(note), "\nLast good %.2f\"", r.last_good);
    if (r.tracker && r.stars >= 0) {
        char st[40];
        fmt_stars(st, sizeof st, r);
        snprintf(note + strlen(note), sizeof note - strlen(note), "\n%s", st);
    }
    if (!isnan(r.snr))
        snprintf(note + strlen(note), sizeof note - strlen(note), "\nSNR %.0f%s", r.snr,
                 strcmp(r.snr_trend, "falling") ? "" : " falling");
    lv_label_set_text(det.sky_note, note);
}

static void update_status_bar() {
    char a[64], t[16];
    long now = now_unix();
    struct tm tmv;
    time_t tt = time(nullptr);
    if (tt > 1700000000 && localtime_r(&tt, &tmv)) {
        strftime(a, sizeof a, "%H:%M", &tmv);
        lv_label_set_text(sb_time, a);
    }
    if (!wifi_up) {
        lv_label_set_text(sb_bridge, LV_SYMBOL_WARNING " No WiFi");
        lv_obj_set_style_text_color(sb_bridge, col(C_RED), 0);
    } else if (!mqtt_up) {
        lv_label_set_text(sb_bridge, LV_SYMBOL_WARNING " No MQTT broker");
        lv_obj_set_style_text_color(sb_bridge, col(C_RED), 0);
    } else if (!bridge_online) {
        lv_label_set_text(sb_bridge, LV_SYMBOL_WARNING " Bridge offline");
        lv_obj_set_style_text_color(sb_bridge, col(C_RED), 0);
    } else {
        // Heartbeats come every 10 s, so a running "x s ago" just resets constantly.
        // Show a time only when one is late; past HEARTBEAT_TIMEOUT_MS it's "Bridge offline".
        long age = heartbeat_ms ? (long)((millis() - heartbeat_ms) / 1000) : -1;
        if (age > 15) {
            snprintf(a, sizeof a, LV_SYMBOL_WARNING " Bridge quiet  %s", fmt_elapsed(age, t, sizeof t));
            lv_obj_set_style_text_color(sb_bridge, col(C_AMBER), 0);
        } else {
            snprintf(a, sizeof a, LV_SYMBOL_OK " Bridge live");
            lv_obj_set_style_text_color(sb_bridge, col(C_TEXT2), 0);
        }
        lv_label_set_text(sb_bridge, a);
    }
    const char *bat = battery_pct < 0 ? "" : battery_pct > 80 ? LV_SYMBOL_BATTERY_FULL
                    : battery_pct > 55 ? LV_SYMBOL_BATTERY_3 : battery_pct > 30 ? LV_SYMBOL_BATTERY_2
                    : battery_pct > 10 ? LV_SYMBOL_BATTERY_1 : LV_SYMBOL_BATTERY_EMPTY;
    if (battery_pct == -2) snprintf(a, sizeof a, "%s  " LV_SYMBOL_USB, wifi_up ? LV_SYMBOL_WIFI : "");
    else if (battery_pct >= 0) snprintf(a, sizeof a, "%s  %s %d%%", wifi_up ? LV_SYMBOL_WIFI : "", bat, battery_pct);
    else snprintf(a, sizeof a, "%s", wifi_up ? LV_SYMBOL_WIFI : "");
    lv_label_set_text(sb_power, a);
    (void)now;
}

// Which rigs get the overview's rows, when there are more rigs than rows. Rigs that are imaging
// (armed) or need attention come first, then the most recently active, then config order. The
// chosen rigs keep config order on screen, so rows only move when the set of rigs changes.
static void pick_rows() {
    if (RIG_COUNT <= NROWS) return;
    int64_t key[RIG_COUNT];
    for (int i = 0; i < RIG_COUNT; i++) {
        const RigView &r = rigs[i];
        Effective e = effective(i);
        int64_t recent = max(r.last_frame_ts, r.guiding_since);
        key[i] = (r.have && (r.armed || e.level >= LVL_WARN)) ? (int64_t)3000000000 + recent
               : r.have ? (int64_t)1000000000 + recent : 0;
    }
    bool chosen[RIG_COUNT] = {};
    for (int n = 0; n < NROWS; n++) {
        int best = -1;
        for (int i = 0; i < RIG_COUNT; i++)
            if (!chosen[i] && (best < 0 || key[i] > key[best])) best = i;   // ties: config order
        chosen[best] = true;
    }
    int slot = 0;
    bool changed = false;
    for (int i = 0; i < RIG_COUNT && slot < NROWS; i++) {
        if (!chosen[i]) continue;
        if (slot_rig[slot] != i) { slot_rig[slot] = i; changed = true; }
        slot++;
    }
    if (changed)
        for (int i = 0; i < RIG_COUNT; i++) rigs[i].dirty = true;
}

void ui_update() {
    static uint32_t last_full = 0;
    bool tick = millis() - last_full > 1000;   // elapsed times count up once a second
    if (tick) last_full = millis();
    update_status_bar();
    if (tick && detail_rig < 0) pick_rows();
    if (detail_rig < 0) {
        for (int s = 0; s < NROWS; s++)
            if (slot_rig[s] >= 0 && (rigs[slot_rig[s]].dirty || tick)) update_row(s);
    } else if (rigs[detail_rig].dirty || tick) {
        if (full_open) update_full(detail_rig); else update_detail(detail_rig);
    }
    for (int i = 0; i < RIG_COUNT; i++)
        if (detail_rig < 0 || detail_rig == i) rigs[i].dirty = rigs[i].trend_dirty = rigs[i].history_dirty = false;
}

void ui_set_dimmed(bool dimmed) { ui_dimmed = dimmed; }

void ui_show_overview() {
    if (detail_rig < 0) return;
    full_open = false;
    detail_rig = -1;
    lv_scr_load(scr_overview);
    for (int i = 0; i < RIG_COUNT; i++) rigs[i].dirty = true;
}
