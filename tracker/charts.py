# Version: 1.3.2
"""Plotly figures from the plot data built in analysis.build_plot_data. Kept separate so the data/analysis code
can be tested without plotly installed."""
from .analysis import SEV_RANK

# Status colours: red = fail, amber = exception, blue = pass (not red/green, so colour-blind readers can tell them
# apart), greys for other/excluded. Five shades each, darkest = most severe, used when splitting by severity.
STATUS_SHADES = {
    "fail": ["#8f1f1f", "#c93c3c", "#e5605f", "#f09a99", "#f7c3c2"],
    "exception": ["#8a5a00", "#b97d00", "#e0a000", "#f0c24d", "#f7dc94"],
    "pass": ["#14407a", "#1f5fae", "#3987e5", "#7fb2ee", "#b4d2f5"],
    "other": ["#4a4a48", "#6e6e6b", "#92928e", "#b5b5b1", "#d3d3cf"],
    "excluded": ["#6b6b68", "#8c8c88", "#a9a9a5", "#c4c4c0", "#dcdcd8"],
}


def series_order(plot):
    """Series names bottom-to-top for stacked charts (analysis puts the sort key in s_ord)."""
    if "s_ord" not in plot:
        return list(dict.fromkeys(plot["series"]))
    return plot.drop_duplicates("series").sort_values("s_ord")["series"].tolist()


def status_colors(series, group_field):
    """Colour map for 'bucket' or 'group · bucket' series; None when shades wouldn't mean anything."""
    out, shade = {}, {}
    for s in series:
        grp, _, bucket = s.rpartition(" · ")
        bucket = bucket if bucket in STATUS_SHADES else s
        if bucket not in STATUS_SHADES:
            return None
        if grp:
            if group_field != "severity":
                return None
            shade[s] = (bucket, SEV_RANK.get(grp.strip().lower(), 4))
        else:
            shade[s] = (bucket, 2)
    for s, (bucket, i) in shade.items():
        out[s] = STATUS_SHADES[bucket][min(i, 4)]
    return out


def make_figure(res):
    import plotly.express as px

    plot, cfg, facet = res["plot"], res["cfg"], res["facet"]
    chart, measure, stack, norm = cfg["chart"], cfg["measure"], cfg["stack"], cfg["norm100"] and "share" in plot
    kw = {"facet_col": "group", "facet_col_wrap": 3} if facet else {}
    hover = {"date_label": False}
    y = "share" if norm else "value"
    if stack != "off":
        order = series_order(plot)
        kw["category_orders"] = {"series": order}
        if measure == "status":
            cmap = status_colors(order, cfg["group"])
            if cmap:
                kw["color_discrete_map"] = cmap
    if chart == "Bar: first vs latest":
        fig = px.bar(plot, x="x", y="value", color="series", barmode="group", hover_data=hover)
        fig.update_xaxes(title=None)
    elif chart == "Bar":
        fig = px.bar(plot, x="date_label", y=y, color="series",
                     barmode="stack" if stack != "off" else "group", hover_data=hover,
                     custom_data=["value", "share"] if norm else None, **kw)
        fig.update_xaxes(type="category", title=None)
    elif chart == "Area":
        fig = px.area(plot, x="as_of_date", y=y, color="series", hover_data=hover,
                      custom_data=["value", "share"] if norm else None, **kw)
        fig.update_xaxes(title=None)
    else:
        fig = px.line(plot, x="as_of_date", y="value", color="series", markers=True, hover_data=hover, **kw)
        fig.update_xaxes(title=None)
    if norm:   # segments add to 100%; the tooltip shows the absolute line count and the share
        fig.update_traces(hovertemplate="<b>%{fullData.name}</b><br>%{x}<br>%{customdata[0]:,.0f} lines "
                                        "(%{customdata[1]:.1f}%)<extra></extra>")
        fig.update_yaxes(range=[0, 100], title="% of lines")
    elif measure == "pct":
        fig.update_yaxes(range=[0, 100], title="% complete")
    else:
        fig.update_yaxes(title="lines")
    if stack != "off":   # legend reads top-to-bottom in the same order as the stack
        fig.update_layout(legend_traceorder="reversed")
    fig.update_layout(legend_title_text="", margin=dict(l=10, r=10, t=30, b=10))
    return fig
