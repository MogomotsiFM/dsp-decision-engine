import typing
import pandas as pd
import numpy as np
from pydantic import BaseModel
from dataclasses import dataclass
from spockflow.components.tree.settings import settings
from .core import ChildTree, ConditionedNode, Tree

if typing.TYPE_CHECKING:
    from hamilton import node

# TODO support variable nodes (Placeholders)
TOutputType = typing.Union[typing.Callable[...,pd.DataFrame], pd.DataFrame, str]
TConditionType = typing.Union[typing.Callable[...,pd.Series], pd.Series, str]
NumpyConditionedNode = ConditionedNode[TOutputType, TConditionType]


class NumpyChildTree(ChildTree[TOutputType,TConditionType,NumpyConditionedNode]):
    @property
    def NodeType(self) -> NumpyConditionedNode:
        return NumpyConditionedNode


class NumpyTree(Tree):
    def __init__(self, doc: str=None, additional_tags: typing.Dict[str, str] = None, _root=None) -> None:
        super().__init__(_root)
        self._compiled_tree = None
        if doc is None:
            doc = "This executes a user defined decision tree"
        self.doc = doc
        if additional_tags is None:
            additional_tags = {}
        self.additional_tags = additional_tags

    @property
    def TreeType(self):
        return NumpyChildTree
    
    def _compile(self):
        if self._compiled_tree is None:
            self._compiled_tree = CompiledNumpyTree(self)
        return self._compiled_tree
    
    def execute(self, **values):
        return self._compile()(**values)
    
    def get_return_type(self):
        return pd.DataFrame
    
    @staticmethod
    def namespaced(namespace: str, fn: typing.Callable):
        # from inspect import Signature
        offset = len(namespace)+1
        def inner(**kwargs):
            return fn(**{k[offset:]: v for k,v in kwargs.items()})
        return inner

    def generate_nodes(self, config: dict, var_name: str=None) -> typing.List["node.Node"]:
        from hamilton import node
        self.set_name(var_name)
        compiled_tree = self._compile()
        node_input_types = {
            o: pd.DataFrame
            for o in compiled_tree.execution_outputs
        }
        node_input_types.update({
            c: typing.Union[np.ndarray,pd.Series]
            for c in compiled_tree.execution_conditions
        })
        TCastData = typing.Tuple[np.ndarray, pd.DataFrame, np.ndarray, np.ndarray, int]
        return [
            node.Node(
                self.name+".cast_data",
                TCastData,
                self.doc,
                callabl=compiled_tree._cast_length_conditions_outputs,
                tags={
                    "module": self.module, 
                    **self.additional_tags
                },
                node_source=node.NodeType.STANDARD,
                input_types = node_input_types
            ),
            node.Node(
                self.name+".conditions_met",
                np.ndarray,
                self.doc,
                callabl=self.namespaced(self.name, compiled_tree._calculate_conditions_met),
                tags={
                    "module": self.module, 
                    **self.additional_tags
                },
                node_source=node.NodeType.STANDARD,
                input_types = {
                    self.name+".cast_data": TCastData
                }
            ),
            node.Node(
                self.name+".condition_names",
                typing.List[str],
                self.doc,
                callabl=self.namespaced(self.name, compiled_tree._all_condition_names),
                tags={
                    "module": self.module, 
                    **self.additional_tags
                },
                node_source=node.NodeType.STANDARD,
                input_types = {
                    self.name+".cast_data": TCastData
                }
            ),
            node.Node(
                self.name,
                self.get_return_type(),
                self.doc,
                callabl=self.namespaced(self.name, compiled_tree.get_results),
                tags={
                    "module": self.module, 
                    **self.additional_tags
                },
                node_source=node.NodeType.STANDARD,
                input_types = {
                    self.name+".cast_data": TCastData,
                    self.name+".conditions_met": np.ndarray,
                }
            ),
            node.Node(
                self.name+".trace",
                self.get_return_type(),
                self.doc,
                callabl=self.namespaced(self.name, compiled_tree.trace),
                tags={
                    "module": self.module, 
                    **self.additional_tags
                },
                node_source=node.NodeType.STANDARD,
                input_types = {
                    self.name+".cast_data": TCastData,
                    self.name+".conditions_met": np.ndarray,
                }
            )
        ]


@dataclass
class ConditionedOutput:
    output: TOutputType
    conditions: typing.Tuple[TConditionType]

@dataclass
class SymbolicConditionedOutput:
    output: str
    conditions: typing.Tuple[str]

@dataclass
class SymbolicFlatTree:
    outputs: typing.Dict[str,TOutputType]
    conditions: typing.Dict[str,TConditionType]
    tree: typing.List[SymbolicConditionedOutput]


class CompiledNumpyTree:
    def __init__(self, tree: NumpyTree) -> None:
        self.length = len(tree.root)

        flattened_tree = self._flatten_tree(
            sub_tree=tree.root,
            current_conditions=tuple(),
            seen=set(),
            conditioned_outputs=[]
        )
        flattened_tree = self._get_symbolic_tree(
            flattened_tree
        )

        (
            predefined_conditions, 
            predefined_condition_names, 
            execution_conditions
        ) = self._split_predefined(
            items=flattened_tree.conditions,
            predefined_types=(pd.Series, np.ndarray),
            error_types=pd.DataFrame,
            error_msg="Condition cannot be a DataFrame.",
            cast_fn=self._cast_condition
        )
        
        if len(predefined_conditions) > 0:
            self.predefined_conditions = np.stack(predefined_conditions).astype(bool)
        else:
            self.predefined_conditions = np.empty([1,0], dtype=bool)
        self.all_condition_names = predefined_condition_names+execution_conditions
        self.execution_conditions = execution_conditions

        (
            predefined_outputs, 
            predefined_outputs_names, 
            execution_outputs
        ) = self._split_predefined(
            items=flattened_tree.outputs,
            predefined_types=pd.DataFrame,
            error_types=(pd.Series, np.ndarray),
            error_msg="Outputs must be a pd.DataFrame, function or str.",
            cast_fn=self._assert_length
        )
        # Add in a empty dataframe as a default (will automatically get the right schema due to pd.concat)
        self.predefined_outputs = pd.concat(predefined_outputs)
        self.all_output_names = predefined_outputs_names+execution_outputs
        self.execution_outputs = execution_outputs
        output_df_lengths = [len(o) for o in predefined_outputs]
        if len(self.execution_outputs) == 0:
            self.predefined_outputs = pd.concat(
                [self._get_empty_data_frame_like(self.predefined_outputs), self.predefined_outputs]
            )
        self.output_df_lengths = [1] + output_df_lengths

        truth_table = np.zeros(
            (len(self.all_output_names)+1,len(self.all_condition_names)), 
            dtype=int
        )

        condition_lookup = {k:i for i,k in enumerate(self.all_condition_names)}
        # Preinserting default output during execution
        output_lookup = {k:i+1 for i,k in enumerate(self.all_output_names)}
        self.ordered_output_map = np.zeros(len(self.all_output_names)+1, dtype=np.uint32)
        self.ordered_output_map[-1] = 0 # Set map to default output
        for ni, n in enumerate(flattened_tree.tree):
            oi = output_lookup[n.output]
            # Use this map to map the tree ordered results into the output ordered results
            self.ordered_output_map[ni] = oi
            for c in n.conditions:
                ci = condition_lookup[c]
                # Use ni to preserve order of original tree
                truth_table[ni,ci] = 1

        # shape [o,c]
        self.truth_table = truth_table.T
        # shape [o,1]
        self.truth_table_thresh = truth_table.T.sum(axis=0,keepdims=True)


    @staticmethod
    def _get_empty_data_frame_like(other: pd.DataFrame)->pd.DataFrame:
        return pd.DataFrame({k:[pd.NA] for k in other.columns})

    def _assert_length(self, v):
        if self.length != 1 and len(v) != 1 and len(v) != self.length:
            raise ValueError(f"All static values must have the same length. Found {len(v)} != {self.length}")
        return v

    def _cast_condition(self, v, length=None):
        if length is None:
            length = self.length
        if isinstance(v, pd.Series):
            v = v.values
        if v.ndim > 1:
            # TODO i think that ndim=0 will cause issues wherever there is a len(v)
            raise ValueError(f"Conditions must be of dimension 0 or 1 not {v.ndim}")
        if length != 1 and len(v) != length:
            if len(v) != 1:
                raise ValueError(f"All static conditions must have the same length. Found {len(v)} != {length}.")
            v = np.repeat(v, repeats=length)
        return v.astype(bool)


    @classmethod
    def _split_predefined(
        cls,
        items: typing.Dict[str,any],
        predefined_types: tuple,
        error_types: tuple,
        error_msg: str,
        cast_fn: typing.Callable
    ) -> SymbolicFlatTree:
        # Split into what is known before hand and what is only known later
        predefined = []
        predefined_names = []
        execution = []
        for k,v in items.items():
            if isinstance(v, predefined_types):
                predefined.append(
                    cast_fn(v)
                )
                predefined_names.append(k)
            elif isinstance(v, error_types):
                # This should never happen as there is already checks into the raw tree
                raise ValueError(error_msg)
            else:
                execution.append(k)
        return (predefined, predefined_names, execution)


    @classmethod
    def _get_symbolic_tree(
        cls,
        conditioned_outputs: typing.List[ConditionedOutput],
    ) -> SymbolicFlatTree:
        from spockflow._util import get_name
        conditions = {}
        outputs = {}

        def get_unique_name(v):
            nonlocal conditions, outputs
            try:
                if isinstance(v, str): 
                    name = v
                else:
                    name = get_name(v, None)
            except ValueError as e:
                if not isinstance(v, (pd.DataFrame, pd.Series, np.ndarray)):
                    raise ValueError(
                        f"Name required for non-statically defined variables.\n"
                        f"Could not find name for {v}."
                    ) from e
                name = "Internal Expression"

            non_unique_template = f"{name} {{duplicate_index:04d}}"
            for duplicate_index in range(settings.max_name_repetition):
                existing_value = conditions.get(name, outputs.get(name, None))
                # Found a name that doesn't exist
                if existing_value is None: return name
                # Found a matching key for existing name
                if existing_value is v:
                    return name
                elif not isinstance(v, (pd.DataFrame, pd.Series, np.ndarray)):
                    # The space in the non_unique template should stop us needing to shift around internal names to cater for external names
                    # exceptions to this might be a series in a condition named the same as a function
                    # Reasonable to expect user to then rename the series or dataframe before continuing
                    raise ValueError(
                        f"External values and conditions must have unique names to allow for parameterization during execution.\n"
                        f"Found duplicate name {name} with value {v} (id:{id(v)}) that is different from {existing_value} (id:{id(existing_value)})."
                    )
                name = non_unique_template.format(duplicate_index=duplicate_index)

        new_tree_nodes = []
        for n in conditioned_outputs:
            # Both cases below it should not matter if the name is overwritten
            # Keeps code cleaner than doing an if name not in 
            new_conditions = []
            for c in n.conditions:
                name = get_unique_name(c)
                conditions[name] = c
                new_conditions.append(name)
            name = get_unique_name(n.output)
            outputs[name] = n.output
            new_tree_nodes.append(SymbolicConditionedOutput(name, tuple(new_conditions)))

        return SymbolicFlatTree(
            outputs=outputs,
            conditions=conditions,
            tree=new_tree_nodes
        )


    @classmethod
    def _flatten_tree(
        cls, 
        sub_tree: NumpyChildTree,
        current_conditions: typing.Tuple[TConditionType],
        seen: typing.Set[int],
        conditioned_outputs: typing.List[ConditionedOutput],
    ) -> typing.List[ConditionedOutput]:
        curr_id = id(sub_tree)
        if curr_id in seen:
            raise ValueError(
                "Current tree contains loops. Cannot compile tree."
            )
        for n in sub_tree.nodes:
            if n.value is None:
                raise ValueError(
                    "All nodes must have a value set to be a valid tree.\n"
                    "Found a leaf with no value."
                )
            if n.condition is None:
                raise ValueError(
                    "All nodes must have a condition set to be a valid tree\n"
                    "Found a leaf with no condition.\n"
                    "If this is intended to be a default value please use set_default."
                )
            
            n_conditions = current_conditions+(n.condition,)
            if isinstance(n.value, ChildTree):
                cls._flatten_tree(
                    n.value,
                    current_conditions=n_conditions,
                    seen=seen.union([curr_id]),
                    conditioned_outputs=conditioned_outputs,
                )
            else:
                conditioned_outputs.append(
                    ConditionedOutput(
                        n.value,
                        n_conditions
                    )
                )
        
        if sub_tree.default_value is not None:
            conditioned_outputs.append(
                ConditionedOutput(
                    sub_tree.default_value,
                    current_conditions
                )
            )

        return conditioned_outputs

    def trace(self, **kwds: typing.Union[pd.DataFrame, pd.Series]) -> pd.DataFrame:
        raise NotImplementedError()
    

    def _cast_length_conditions_outputs(self, **kwds: typing.Union[pd.DataFrame, pd.Series]) -> typing.Tuple[np.ndarray, pd.DataFrame, np.ndarray, np.ndarray, int]:
        length = self.length
        if self.execution_outputs:

            execution_lengths = []
            for o in self.execution_outputs:
                len_o = len(kwds[o])
                is_length = len_o != 1
                execution_lengths.append(is_length)
                if is_length:
                    if length == 1: length = len_o
                    elif length != len_o:
                        raise ValueError(f"All output values must have the same length. Found {len_o} != {length}.")
                    
            outputs = pd.concat(
                [self.predefined_outputs]+
                [kwds[o] for o in self.execution_outputs]
            )
            # Add in the default row now that the output schema should be known
            outputs = pd.concat(
                [self._get_empty_data_frame_like(outputs), outputs]
            )
            output_df_lengths = self.output_df_lengths+execution_lengths
        else:
            # This should be a common case so adding this statement should add a speedup to many trees
            outputs = self.predefined_outputs
            output_df_lengths = self.output_df_lengths
        output_start_offsets = np.cumsum([0]+output_df_lengths)
        output_df_lengths = np.array(output_df_lengths)
        # Calculate inputs
        for c in self.execution_conditions:
            len_c = len(kwds[c])
            if len_c != 1:
                if length == 1: length = len_c
                elif length != len_c:
                    raise ValueError(f"All output values must have the same length. Found {len_c} != {length}.")
        
        predefined_conditions=self.predefined_conditions
        if self.predefined_conditions.shape[0] == 1:
            predefined_conditions=predefined_conditions.repeat(length,axis=0)        
        # if predefined_conditions.shape[1] == 0:
        #     predefined_conditions = []
        # else:
        #     predefined_conditions = [predefined_conditions]
        conditions = np.hstack(
            [predefined_conditions]+
            [self._cast_condition(kwds[c], length)[:,None] for c in self.execution_conditions]
        )
        return conditions, outputs, output_df_lengths, output_start_offsets, length

    def _calculate_conditions_met(
            self, 
            cast_data: typing.Tuple[np.ndarray, pd.DataFrame, np.ndarray, np.ndarray, int]
        ) -> pd.DataFrame:
        # Calculate outputs
        conditions = cast_data[0]
        # [O,C]@[C,N] => [O,N] (Matrix multiplication should be the same as performing a count of all true statements)
        # The thresh will see where they are all true
        return (conditions@self.truth_table) >= self.truth_table_thresh
    
    def _all_condition_names(
            self, 
            cast_data: typing.Tuple[np.ndarray, pd.DataFrame, np.ndarray, np.ndarray, int]
        ) -> typing.List[str]:
        conditions = cast_data[0]
        res = []
        for c in conditions:
            res.append(",".join(v for v,t in zip(self.all_condition_names,c) if t))
        return res

    def get_results(
            self,
            cast_data: typing.Tuple[np.ndarray, pd.DataFrame, np.ndarray, np.ndarray, int],
            conditions_met: np.ndarray,
        ) -> pd.DataFrame:
        _, outputs, output_df_lengths, output_start_offsets, length = cast_data
        condition_output_idx = np.argmax(conditions_met,axis=1)
        # Translate outputs from tree lookup to outputs from combined outputs.
        ordered_output_idx = self.ordered_output_map[condition_output_idx]
        # Lookup values that are offset from the start index
        item_index = np.arange(length)
        output_idx = output_start_offsets[ordered_output_idx]+(output_df_lengths[ordered_output_idx]>1)*item_index
        return outputs.iloc[output_idx].reset_index(drop=True)

    
    def __call__(self, **kwds: typing.Union[pd.DataFrame, pd.Series]) -> pd.DataFrame:
        cast_data = self._cast_length_conditions_outputs(**kwds)
        conditions_met = self._calculate_conditions_met(cast_data)
        return self.get_results(cast_data, conditions_met)
