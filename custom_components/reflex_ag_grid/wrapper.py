"""ag-grid rx.model wrapper."""

import datetime
import json
from typing import Any, ClassVar, Generic, Type

from fastapi import Request
from sqlmodel import col, func, select

import reflex as rx

from reflex_ag_grid.ag_grid import ag_grid, ColumnDef
from reflex_ag_grid.datasource import Datasource
from reflex_ag_grid.handlers import apply_filter_model, apply_sort_model, M


def _value_setter_signature(
    params: rx.Var[dict[str, Any]],
) -> tuple[rx.Var[int], rx.Var[str], rx.Var[Any]]:
    return (
        params["data"],
        params["colDef"].to(dict)["field"],
        params["newValue"],
    )


def get_default_column_def(
    field: str, ftype: Type, value_setter: rx.EventHandler | None = None, **cdef_kwargs
) -> ColumnDef:
    """Get a default column definition for a given field type.

    Args:
        field: The field name.
        ftype: The field type.
        value_setter: The value setter event handler.
        **cdef_kwargs: Additional ColumnDef keyword arguments.

    Returns:
        The column definition.
    """
    _cdef_kwargs = dict(
        sortable=True,
        filter=True,
        editable=True if value_setter is not None else False,
        cell_editor=ag_grid.editors.text,
    )
    _cdef_kwargs.update(cdef_kwargs)
    cdef = ag_grid.column_def(
        field=field,
        **_cdef_kwargs,
    )
    if rx.utils.types.is_optional(ftype):
        ftype = rx.utils.types.args(ftype)[0]
    if ftype in (int, float, bool):
        cdef.type = "numericColumn"
        cdef.filter = ag_grid.filters.number
        cdef.cell_editor = ag_grid.editors.number
    if ftype is bool:
        cdef.cell_renderer = "agCheckboxCellRenderer"
        cdef.cell_editor = ag_grid.editors.checkbox
    if ftype is datetime.datetime:
        cdef.filter = ag_grid.filters.date
        cdef.cell_editor = ag_grid.editors.date
    if value_setter is not None:
        cdef.value_setter = rx.EventChain(
            events=[
                rx.event.call_event_handler(value_setter, _value_setter_signature),
            ],
            args_spec=_value_setter_signature,
            # XXX: hacks to queue events from call_script eval context
            invocation=rx.vars.function.FunctionStringVar(
                "((events) => {queueEvents(events, socket); processEvent(socket)})",
            ),
        )
    return cdef


class AbstractWrapper(rx.ComponentState):
    """Abstract class for wrapping ag-grid for infinite data model."""

    _grid_component: ClassVar[rx.Component | None] = None
    _selected_items: list[Any] = []

    __data_route__ = "/abstract-wrapper-data"
    __get_data_kwargs__ = {
        "state": lambda cls: cls.get_full_name(),
        "start": "${params.startRow}",
        "end": "${params.endRow}",
        "sort_model": "${encodeURIComponent(JSON.stringify(params.sortModel))}",
        "filter_model": "${encodeURIComponent(JSON.stringify(params.filterModel))}",
    }

    @classmethod
    def _add_data_route(cls):
        """Add the backend __data_route__ that responds to ag-grid data requests.

        The backend route will call the _get_data method to fetch the data.
        """
        app = rx.utils.prerequisites.get_app().app
        if cls.__data_route__ in app.api.routes:
            return

        @app.api.get(cls.__data_route__)
        async def get_data(
            request: Request,
            state: str,
            start: int,
            end: int,
            filter_model: str = None,
            sort_model: str = None,
        ):
            try:
                token = request.headers["X-Reflex-Client-Token"]
            except KeyError:
                return []
            if filter_model is not None:
                filter_model = json.loads(filter_model)
            if sort_model is not None:
                sort_model = json.loads(sort_model)
            state_cls = rx.State.get_class_substate(tuple(state.split(".")))
            async with app.modify_state(
                rx.state._substate_key(token, state_cls)
            ) as state:
                s_instance = await state.get_state(state_cls)
                result = s_instance._get_data(
                    start=start,
                    end=end,
                    filter_model=filter_model,
                    sort_model=sort_model,
                )
                if hasattr(result, "__await__"):
                    result = await result
                return result

    @classmethod
    def _get_datasource_uri(cls) -> str:
        """Get the uri for the ag-grid DataSource model."""
        return (
            cls.__data_route__
            + "?"
            + "&".join(
                f"{key}={value if not callable(value) else value(cls)}"
                for key, value in cls.__get_data_kwargs__.items()
            )
        )

    def on_mount(self):
        """Perform post-hydration grid initialization.

        Set up column defs and data source to fetch infinite row data.
        """
        return [
            self._grid_component.api.set_grid_option(
                "columnDefs", self._get_column_defs()
            ),
            self._grid_component.set_datasource(
                Datasource(
                    uri=self._get_datasource_uri(),
                    rowCount=self._row_count(),
                ),
            ),
        ]

    def on_selection_changed(self, rows, source, type):
        """Handle selection changed event."""
        self._selected_items = rows

    def on_value_setter(self, row_data: dict[str, Any], field_name: str, value: Any):
        """Handle setting value in the model."""
        raise NotImplementedError("Handle setting value in the model.")

    def _get_column_defs(self) -> list[ColumnDef]:
        """Get the column definitions for the grid, must be overridden."""
        raise NotImplementedError("Handle fetching column definitions.")

    def _get_data(
        self,
        start: int,
        end: int,
        filter_model: dict[str, Any] | None = None,
        sort_model: list[dict[str, str]] | None = None,
    ) -> list[dict[str, Any]]:
        """Get the data for the grid, must be overridden."""
        raise NotImplementedError("Handle fetching data from the model.")

    def _row_count(self) -> int:
        """Get the total row count for the grid, must be overridden."""
        raise NotImplementedError("Handle fetching row count.")

    @classmethod
    def get_component(cls, *children, **props) -> rx.Component:
        """Return the Ag-Grid component linked to the wrapper state.

        Args:
            children: The children components passed to ag_grid, typically not used.
            **props: Additional props for the ag_grid component.

        Note that "column_defs", "row_model_type", "on_mount", and
        "on_selection_change" cannot be passed here.

        Returns:
            The Ag-Grid component.
        """
        _props = dict(
            id=f"ag-grid-{cls.get_full_name()}",
            default_col_def={"flex": 1},
            max_blocks_in_cache=4,
            cache_block_size=50,
            group_default_expanded=None,
        )
        _props.update(props)
        return ag_grid.root(
            *children,
            row_model_type="infinite",
            on_mount=cls.on_mount,
            on_selection_changed=cls.on_selection_changed,
            **_props,
        )

    @classmethod
    def create(cls, *children, **props) -> rx.Component:
        """Create the ComponentState instance.

        Args:
            children: The children components passed to ag_grid, typically not used.
            **props: Additional props for the ag_grid component.

        Note that "column_defs", "row_model_type", "on_mount", and
        "on_selection_change" cannot be passed here.

        Returns:
            The Ag-Grid component linked to the wrapper state.
        """
        comp = super().create(*children, **props)
        comp.State._grid_component = comp
        comp.State._add_data_route()
        return comp


class ModelWrapper(AbstractWrapper, Generic[M]):
    """Ag-Grid wrapper for arbitrary rx.Model class."""

    _model_class: ClassVar[Type[M] | None] = None
    _selected_items: list[M] = []

    def on_selection_changed(self, rows, source, type):
        self._selected_items = [self._model_class(**row) for row in rows]

    def on_value_setter(self, row_data: dict[str, Any], field_name: str, value: Any):
        try:
            if self._model_class.__fields__[field_name].type_ == datetime.datetime:
                value = datetime.datetime.fromisoformat(value)
        except KeyError:
            pass
        with rx.session() as session:
            item_orm = session.get(self._model_class, row_data["id"])
            if item_orm is not None:
                setattr(item_orm, field_name, value)
                session.add(item_orm)
                session.commit()
                return self._grid_component.api.refreshInfiniteCache()

    def _get_column_defs(self) -> list[ColumnDef]:
        return [
            get_default_column_def(
                field=field.name,
                ftype=field.type_,
                value_setter=type(self).on_value_setter,
                editable=True if field.name != "id" else False,
            )
            for field in self._model_class.__fields__.values()
        ]

    def _row_count(self) -> int:
        with rx.session() as session:
            return session.exec(select(func.count(col(self._model_class.id)))).one()

    def _get_data(
        self,
        start: int,
        end: int,
        filter_model: dict[str, Any] | None = None,
        sort_model: list[dict[str, str]] | None = None,
    ) -> list[M]:
        with rx.session() as session:
            return session.exec(
                apply_sort_model(
                    model=self._model_class,
                    query=apply_filter_model(
                        model=self._model_class,
                        filter_model=filter_model or {},
                    ),
                    sort_model=sort_model or [],
                )
                .offset(start)
                .limit(end - start)
            ).all()

    @classmethod
    def create(cls, *children, model_class: Type[M], **props) -> rx.Component:
        comp = super().create(*children, **props)
        comp.State._model_class = model_class
        return comp


model_wrapper = ModelWrapper.create
