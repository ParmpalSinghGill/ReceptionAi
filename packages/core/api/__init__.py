"""Wire-shape primitives shared by every API surface.

Lives in ``packages.core`` rather than ``apps.api`` because the shapes here are
part of the contract, not of one application's implementation of it — and
``core`` is a leaf, so nothing it declares can drag a persistence or provider
dependency into a caller.
"""
