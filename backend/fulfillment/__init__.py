"""Fulfillment-time ink-split (fund-vista #147, shop #1079).

When an order is received the plotter needs **one SVG per distinct ink**
(one pen pass per file). The buyer flow stays untouched: browsing renders
exactly one draft and one convert; the files here are generated once per
actual order from the layer-grouped draft the renderers already emit.
"""
