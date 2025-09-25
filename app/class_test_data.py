defs = [
    {
        "id": "def:query_to_mongo",
        "symbol": "query_to_mongo",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/__init__.py",
        "signature": "query_to_mongo(query, case_sensitive=True)",
        "summary": "Parse a DictQuery string and return the equivalent MongoDB query dict.",
        "neighbors_up": ["data/dictquery/setup.py"],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py", "data/dictquery/dictquery/parsers.py"]
    },
    {
        "id": "def:compile",
        "symbol": "compile",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/__init__.py",
        "signature": "compile(query, use_nested_keys=True, key_separator='.', case_sensitive=True, raise_keyerror=False)",
        "summary": "Parse the query and return a configured, reusable DataQueryVisitor.",
        "neighbors_up": ["data/dictquery/setup.py"],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py", "data/dictquery/dictquery/parsers.py"]
    },
    {
        "id": "def:match",
        "symbol": "match",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/__init__.py",
        "signature": "match(data, query)",
        "summary": "Return True if the given data object satisfies the query.",
        "neighbors_up": ["data/dictquery/setup.py"],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py", "data/dictquery/dictquery/parsers.py"]
    },
    {
        "id": "def:filter",
        "symbol": "filter",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/__init__.py",
        "signature": "filter(data, query, use_nested_keys=True, key_separator='.', case_sensitive=True, raise_keyerror=False)",
        "summary": "Iterate over items in an iterable and yield those that satisfy the query.",
        "neighbors_up": ["data/dictquery/setup.py"],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py", "data/dictquery/dictquery/parsers.py"]
    },
    {
        "id": "def:query_value",
        "symbol": "query_value",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/datavalue.py",
        "signature": "query_value(data, data_key, use_nested_keys=True, key_separator='.', raise_keyerror=False)",
        "summary": "Traverse dicts/objects/iterables to collect values for a (possibly nested) key; optionally raises DQKeyError if nothing found.",
        "neighbors_up": ["data/dictquery/dictquery/parsers.py", "data/dictquery/dictquery/visitors.py"],
        "neighbors_down": ["data/dictquery/dictquery/exceptions.py"]
    },
    {
        "id": "def:item_factory",
        "symbol": "item_factory",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/datavalue.py",
        "signature": "item_factory(value)",
        "summary": "Return an AbsItem adapter for a mapping, instance, or iterable value; None if unsupported.",
        "neighbors_up": ["data/dictquery/dictquery/datavalue.py"],
        "neighbors_down": ["data/dictquery/dictquery/datavalue.py"]
    },
    {
        "id": "class:DataQueryItem",
        "symbol": "DataQueryItem",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/datavalue.py",
        "signature": "DataQueryItem(key, values, case_sensitive=True, strategy=any)",
        "summary": "Wrap a set of candidate values and provide comparison operators, membership, regex match, and fnmatch-style like with optional case sensitivity and aggregation strategy.",
        "neighbors_up": ["data/dictquery/dictquery/parsers.py", "data/dictquery/dictquery/visitors.py"],
        "neighbors_down": []
    },
    {
        "id": "class:AbsItem",
        "symbol": "AbsItem",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/datavalue.py",
        "signature": "AbsItem(value)",
        "summary": "Abstract adapter that exposes get_value(key) over heterogeneous container types.",
        "neighbors_up": ["data/dictquery/dictquery/datavalue.py"],
        "neighbors_down": []
    },
    {
        "id": "class:InstanceItem",
        "symbol": "InstanceItem",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/datavalue.py",
        "signature": "InstanceItem(value)",
        "summary": "Adapter for Python object instances; yields attribute values for a key or raises KeyError.",
        "neighbors_up": ["data/dictquery/dictquery/datavalue.py"],
        "neighbors_down": []
    },
    {
        "id": "class:MappingItem",
        "symbol": "MappingItem",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/datavalue.py",
        "signature": "MappingItem(value)",
        "summary": "Adapter for mappings; yields mapping[key] or raises KeyError.",
        "neighbors_up": ["data/dictquery/dictquery/datavalue.py"],
        "neighbors_down": []
    },
    {
        "id": "class:IterableItem",
        "symbol": "IterableItem",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/datavalue.py",
        "signature": "IterableItem(value)",
        "summary": "Adapter for iterables; iterates elements and yields nested mapping/instance values for the given key, skipping missing keys.",
        "neighbors_up": ["data/dictquery/dictquery/datavalue.py"],
        "neighbors_down": []
    },
    {
        "id": "def:_is_instance",
        "symbol": "_is_instance",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/datavalue.py",
        "signature": "_is_instance(obj)",
        "summary": "Heuristically detect custom class instances (has dict or slots).",
        "neighbors_up": ["data/dictquery/dictquery/datavalue.py"],
        "neighbors_down": []
    },
    {
        "id": "def:_is_iterable",
        "symbol": "_is_iterable",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/datavalue.py",
        "signature": "_is_iterable(obj)",
        "summary": "Check if object is an Iterable/Sequence.",
        "neighbors_up": ["data/dictquery/dictquery/datavalue.py"],
        "neighbors_down": []
    },
    {
        "id": "def:_is_mapping",
        "symbol": "_is_mapping",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/datavalue.py",
        "signature": "_is_mapping(obj)",
        "summary": "Check if object is a Mapping.",
        "neighbors_up": ["data/dictquery/dictquery/datavalue.py"],
        "neighbors_down": []
    },
    {
        "id": "def:_get_instance_value",
        "symbol": "_get_instance_value",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/datavalue.py",
        "signature": "_get_instance_value(obj, key)",
        "summary": "Return obj.key via getattr or raise KeyError with a formatted message.",
        "neighbors_up": ["data/dictquery/dictquery/datavalue.py"],
        "neighbors_down": []
    },
    {
        "id": "def:_get_mapping_value",
        "symbol": "_get_mapping_value",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/datavalue.py",
        "signature": "_get_mapping_value(obj, key)",
        "summary": "Return obj[key] from a mapping.",
        "neighbors_up": ["data/dictquery/dictquery/datavalue.py"],
        "neighbors_down": []
    },
    {
        "id": "class:DataQueryParser",
        "symbol": "DataQueryParser",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "DataQueryParser()",
        "summary": "Recursive-descent parser for the DictQuery language; turns a token stream into an expression AST using operator precedence (OR/AND/NOT) and value rules.",
        "neighbors_up": ["data/dictquery/dictquery/tokenizer.py", "data/dictquery/dictquery/exceptions.py"],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py"]
    },
    {
        "id": "method:DataQueryParser.parse",
        "symbol": "DataQueryParser.parse",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "parse(query)",
        "summary": "Entry point: trims the query, tokenizes it, and parses into an AST (or None for empty input).",
        "neighbors_up": ["data/dictquery/dictquery/tokenizer.py", "data/dictquery/dictquery/exceptions.py"],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py"]
    },
    {
        "id": "method:DataQueryParser.orstatement",
        "symbol": "DataQueryParser.orstatement",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "orstatement()",
        "summary": "Parse left-associative OR chains of AND statements; enforces that only AND/OR/RPAR may follow.",
        "neighbors_up": ["data/dictquery/dictquery/exceptions.py"],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py"]
    },
    {
        "id": "method:DataQueryParser.andstatement",
        "symbol": "DataQueryParser.andstatement",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "andstatement()",
        "summary": "Parse left-associative AND chains of expressions.",
        "neighbors_up": [],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py"]
    },
    {
        "id": "method:DataQueryParser.expression",
        "symbol": "DataQueryParser.expression",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "expression()",
        "summary": "Parse an optional NOT followed by an expr, producing a NotExpression if present.",
        "neighbors_up": [],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py"]
    },
    {
        "id": "method:DataQueryParser.expr",
        "symbol": "DataQueryParser.expr",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "expr()",
        "summary": "Parse a parenthesized orstatement or a binary/key/value expression including MATCH/LIKE/IN and comparison operators; raises DQSyntaxError on malformed sequences.",
        "neighbors_up": ["data/dictquery/dictquery/exceptions.py"],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py"]
    },
    {
        "id": "method:DataQueryParser.value",
        "symbol": "DataQueryParser.value",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "value()",
        "summary": "Parse a KEY, STRING, REGEXP, primitive literal (BOOLEAN/NUMBER/NONE/NOW), or an array; normalizes quoted KEY/STRING/REGEXP by stripping delimiters.",
        "neighbors_up": ["data/dictquery/dictquery/exceptions.py"],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py"]
    },
    {
        "id": "method:DataQueryParser.array",
        "symbol": "DataQueryParser.array",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "array()",
        "summary": "Parse an array literal [ value, ... ] (including empty lists) and wrap as ArrayExpression.",
        "neighbors_up": [],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py"]
    },
    {
        "id": "method:DataQueryParser._advance",
        "symbol": "DataQueryParser._advance",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "_advance()",
        "summary": "Advance the token window: shift nexttok into tok and pull the next token from the generator.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:DataQueryParser._accept",
        "symbol": "DataQueryParser._accept",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "_accept(toktype)",
        "summary": "If the upcoming token matches a type (or any from a tuple/list), consume it and return True; else False.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:DataQueryParser._expect",
        "symbol": "DataQueryParser._expect",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "_expect(toktype)",
        "summary": "Require a specific token type; consumes it or raises DQSyntaxError.",
        "neighbors_up": ["data/dictquery/dictquery/exceptions.py"],
        "neighbors_down": []
    },
    {
        "id": "class:LiteralExpression",
        "symbol": "LiteralExpression",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "LiteralExpression(value)",
        "summary": "Base AST node for literal values; defines accept() to dispatch to visitor.visit_literal.",
        "neighbors_up": [],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py"]
    },
    {
        "id": "class:UnaryExpression",
        "symbol": "UnaryExpression",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "UnaryExpression(value)",
        "summary": "Base AST node for unary operators like NOT; accept() dispatches to visitor.visit_unary.",
        "neighbors_up": [],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py"]
    },
    {
        "id": "class:BinaryExpression",
        "symbol": "BinaryExpression",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "BinaryExpression(left, right)",
        "summary": "Base AST node for binary operators (e.g., AND, OR, comparisons); accept() dispatches to visitor.visit_binary.",
        "neighbors_up": [],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py"]
    },
    {
        "id": "class:NumberExpression",
        "symbol": "NumberExpression",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "NumberExpression(value)",
        "summary": "Literal number node; visitor hook visit_number.",
        "neighbors_up": [],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py"]
    },
    {
        "id": "class:BooleanExpression",
        "symbol": "BooleanExpression",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "BooleanExpression(value)",
        "summary": "Literal boolean node; visitor hook visit_boolean.",
        "neighbors_up": [],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py"]
    },
    {
        "id": "class:NoneExpression",
        "symbol": "NoneExpression",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "NoneExpression(value)",
        "summary": "Literal None node; visitor hook visit_none.",
        "neighbors_up": [],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py"]
    },
    {
        "id": "class:NowExpression",
        "symbol": "NowExpression",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "NowExpression(value)",
        "summary": "Literal NOW node; visitor hook visit_now (usually resolved to current datetime downstream).",
        "neighbors_up": [],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py"]
    },
    {
        "id": "class:StringExpression",
        "symbol": "StringExpression",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "StringExpression(value)",
        "summary": "Literal string node; visitor hook visit_string.",
        "neighbors_up": [],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py"]
    },
    {
        "id": "class:KeyExpression",
        "symbol": "KeyExpression",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "KeyExpression(value)",
        "summary": "Represents a field/key reference; visitor hook visit_key.",
        "neighbors_up": [],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py", "data/dictquery/dictquery/datavalue.py"]
    },
    {
        "id": "class:RegexpExpression",
        "symbol": "RegexpExpression",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "RegexpExpression(value)",
        "summary": "Literal regular-expression node; visitor hook visit_regexp.",
        "neighbors_up": [],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py"]
    },
    {
        "id": "class:ArrayExpression",
        "symbol": "ArrayExpression",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "ArrayExpression(value)",
        "summary": "Literal array node (list of values/expressions); visitor hook visit_array.",
        "neighbors_up": [],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py", "data/dictquery/dictquery/datavalue.py"]
    },
    {
        "id": "class:InExpression",
        "symbol": "InExpression",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "InExpression(left, right)",
        "summary": "Binary IN operator node (key IN array/string/key). Visitor hook visit_in.",
        "neighbors_up": [],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py"]
    },
    {
        "id": "class:EqualExpression",
        "symbol": "EqualExpression",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "EqualExpression(left, right)",
        "summary": "Binary equality operator node (==). Visitor hook visit_equal.",
        "neighbors_up": [],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py"]
    },
    {
        "id": "class:NotEqualExpression",
        "symbol": "NotEqualExpression",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "NotEqualExpression(left, right)",
        "summary": "Binary inequality operator node (!=). Visitor hook visit_notequal.",
        "neighbors_up": [],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py"]
    },
    {
        "id": "class:MatchExpression",
        "symbol": "MatchExpression",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "MatchExpression(left, right)",
        "summary": "Binary regex match node (MATCH /r/). Visitor hook visit_match.",
        "neighbors_up": [],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py", "data/dictquery/dictquery/datavalue.py"]
    },
    {
        "id": "class:LikeExpression",
        "symbol": "LikeExpression",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "LikeExpression(left, right)",
        "summary": "Binary wildcard match node (LIKE). Visitor hook visit_like.",
        "neighbors_up": [],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py", "data/dictquery/dictquery/datavalue.py"]
    },
    {
        "id": "class:ContainsExpression",
        "symbol": "ContainsExpression",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "ContainsExpression(left, right)",
        "summary": "Binary containment node (CONTAINS). Visitor hook visit_contains.",
        "neighbors_up": [],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py", "data/dictquery/dictquery/datavalue.py"]
    },
    {
        "id": "class:LTExpression",
        "symbol": "LTExpression",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "LTExpression(left, right)",
        "summary": "Binary less-than node (<). Visitor hook visit_lt.",
        "neighbors_up": [],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py", "data/dictquery/dictquery/datavalue.py"]
    },
    {
        "id": "class:LTEExpression",
        "symbol": "LTEExpression",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "LTEExpression(left, right)",
        "summary": "Binary less-than-or-equal node (<=). Visitor hook visit_lte.",
        "neighbors_up": [],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py", "data/dictquery/dictquery/datavalue.py"]
    },
    {
        "id": "class:GTExpression",
        "symbol": "GTExpression",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "GTExpression(left, right)",
        "summary": "Binary greater-than node (>). Visitor hook visit_gt.",
        "neighbors_up": [],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py", "data/dictquery/dictquery/datavalue.py"]
    },
    {
        "id": "class:GTEExpression",
        "symbol": "GTEExpression",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "GTEExpression(left, right)",
        "summary": "Binary greater-than-or-equal node (>=). Visitor hook visit_gte.",
        "neighbors_up": [],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py", "data/dictquery/dictquery/datavalue.py"]
    },
    {
        "id": "class:AndExpression",
        "symbol": "AndExpression",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "AndExpression(left, right)",
        "summary": "Logical AND node; visitor hook visit_and.",
        "neighbors_up": [],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py"]
    },
    {
        "id": "class:OrExpression",
        "symbol": "OrExpression",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "OrExpression(left, right)",
        "summary": "Logical OR node; visitor hook visit_or.",
        "neighbors_up": [],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py"]
    },
    {
        "id": "class:NotExpression",
        "symbol": "NotExpression",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/parsers.py",
        "signature": "NotExpression(value)",
        "summary": "Logical NOT node; visitor hook visit_not.",
        "neighbors_up": [],
        "neighbors_down": ["data/dictquery/dictquery/visitors.py"]
    },
    {
        "id": "type:Token",
        "symbol": "Token",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/tokenizer.py",
        "signature": "Token = namedtuple('Token', ['type', 'value'])",
        "summary": "Lightweight token record with fields 'type' and 'value' produced by the tokenizer.",
        "neighbors_up": ["data/dictquery/dictquery/exceptions.py"],
        "neighbors_down": ["data/dictquery/dictquery/parsers.py"]
    },
    {
        "id": "var:token_specification",
        "symbol": "token_specification",
        "kind": "variable",
        "file_path": "data/dictquery/dictquery/tokenizer.py",
        "signature": "token_specification = [(name, pattern), ...]",
        "summary": "Ordered list of (token_name, regex) pairs defining the DictQuery lexical grammar (numbers, booleans, strings, parens/brackets, operators, keywords, commas, regex literals, identifiers/keys, whitespace, and mismatch).",
        "neighbors_up": [],
        "neighbors_down": ["data/dictquery/dictquery/parsers.py"]
    },
    {
        "id": "var:tok_regex",
        "symbol": "tok_regex",
        "kind": "variable",
        "file_path": "data/dictquery/dictquery/tokenizer.py",
        "signature": "tok_regex = re.compile('|'.join('(?P<%s>%s)' % pair for pair in token_specification), re.IGNORECASE)",
        "summary": "Combined, case-insensitive master regex with named groups for each token type built from token_specification.",
        "neighbors_up": [],
        "neighbors_down": ["data/dictquery/dictquery/parsers.py"]
    },
    {
        "id": "def:gen_tokens",
        "symbol": "gen_tokens",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/tokenizer.py",
        "signature": "gen_tokens(text, skip_ws=True)",
        "summary": "Scan input text with tok_regex and yield Token(type, value) for each match; skips whitespace when skip_ws is True and raises DQSyntaxError on mismatches.",
        "neighbors_up": ["data/dictquery/dictquery/exceptions.py"],
        "neighbors_down": ["data/dictquery/dictquery/parsers.py"]
    },
    {
        "id": "class:DataQueryVisitor",
        "symbol": "DataQueryVisitor",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "DataQueryVisitor(ast, use_nested_keys=True, key_separator='.', case_sensitive=True, raise_keyerror=False)",
        "summary": "Default evaluator for the DictQuery AST. Walks the AST against a Python data object and returns True/False depending on whether the data matches the query.",
        "neighbors_up": ["data/dictquery/dictquery/parsers.py", "data/dictquery/dictquery/datavalue.py", "data/dictquery/dictquery/exceptions.py"],
        "neighbors_down": ["data/dictquery/dictquery/init.py"]
    },
    {
        "id": "method:DataQueryVisitor.evaluate",
        "symbol": "DataQueryVisitor.evaluate",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "evaluate(data)",
        "summary": "Evaluate the stored AST against the provided data object; returns a boolean. Safely sets/clears internal self.data.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:DataQueryVisitor.match",
        "symbol": "DataQueryVisitor.match",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "match(data)",
        "summary": "Alias for evaluate(data); kept for a matcher-style API.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:DataQueryVisitor._get_dict_value",
        "symbol": "DataQueryVisitor._get_dict_value",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "_get_dict_value(dict_key)",
        "summary": "Internal helper: extract values for a (possibly nested) key from self.data via query_value; enforces that self.data is set.",
        "neighbors_up": ["data/dictquery/dictquery/datavalue.py", "data/dictquery/dictquery/exceptions.py"],
        "neighbors_down": []
    },
    {
        "id": "method:DataQueryVisitor.visit_lt",
        "symbol": "DataQueryVisitor.visit_lt",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_lt(expr)",
        "summary": "Evaluate a '<' comparison using Python's operator.lt.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:DataQueryVisitor.visit_lte",
        "symbol": "DataQueryVisitor.visit_lte",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_lte(expr)",
        "summary": "Evaluate a '<=' comparison using operator.le.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:DataQueryVisitor.visit_gt",
        "symbol": "DataQueryVisitor.visit_gt",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_gt(expr)",
        "summary": "Evaluate a '>' comparison using operator.gt.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:DataQueryVisitor.visit_gte",
        "symbol": "DataQueryVisitor.visit_gte",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_gte(expr)",
        "summary": "Evaluate a '>=' comparison using operator.ge.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:DataQueryVisitor.visit_equal",
        "symbol": "DataQueryVisitor.visit_equal",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_equal(expr)",
        "summary": "Evaluate equality using operator.eq.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:DataQueryVisitor.visit_notequal",
        "symbol": "DataQueryVisitor.visit_notequal",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_notequal(expr)",
        "summary": "Evaluate inequality using operator.ne.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:DataQueryVisitor.visit_contains",
        "symbol": "DataQueryVisitor.visit_contains",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_contains(expr)",
        "summary": "Evaluate containment 'left CONTAINS right' via operator.contains(left, right).",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:DataQueryVisitor.visit_in",
        "symbol": "DataQueryVisitor.visit_in",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_in(expr)",
        "summary": "Evaluate membership 'left IN right' via operator.contains(right, left).",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:DataQueryVisitor.visit_match",
        "symbol": "DataQueryVisitor.visit_match",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_match(expr)",
        "summary": "Evaluate regex match. If the left side supports .match(), delegate; otherwise use compiled regex.match(left).",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:DataQueryVisitor.visit_like",
        "symbol": "DataQueryVisitor.visit_like",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_like(expr)",
        "summary": "Evaluate wildcard pattern using fnmatch.fnmatchcase unless left supports .like().",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:DataQueryVisitor.visit_key",
        "symbol": "DataQueryVisitor.visit_key",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_key(expr)",
        "summary": "Build a DataQueryItem for the given key using values extracted from self.data (respects case sensitivity).",
        "neighbors_up": ["data/dictquery/dictquery/datavalue.py"],
        "neighbors_down": []
    },
    {
        "id": "method:DataQueryVisitor.visit_number",
        "symbol": "DataQueryVisitor.visit_number",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_number(expr)",
        "summary": "Convert numeric literal text to float.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:DataQueryVisitor.visit_boolean",
        "symbol": "DataQueryVisitor.visit_boolean",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_boolean(expr)",
        "summary": "Return True if literal text equals 'true' (case-insensitive).",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:DataQueryVisitor.visit_string",
        "symbol": "DataQueryVisitor.visit_string",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_string(expr)",
        "summary": "Return string literal, optionally lowercased when case_sensitive is False.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:DataQueryVisitor.visit_now",
        "symbol": "DataQueryVisitor.visit_now",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_now(expr)",
        "summary": "Return current UTC datetime.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:DataQueryVisitor.visit_none",
        "symbol": "DataQueryVisitor.visit_none",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_none(expr)",
        "summary": "Return None.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:DataQueryVisitor.visit_regexp",
        "symbol": "DataQueryVisitor.visit_regexp",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_regexp(expr)",
        "summary": "Compile a regular expression; honors case_sensitive by adding re.IGNORECASE when False.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:DataQueryVisitor.visit_array",
        "symbol": "DataQueryVisitor.visit_array",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_array(expr)",
        "summary": "Evaluate each element in an array literal and return the resulting Python list.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:DataQueryVisitor.visit_not",
        "symbol": "DataQueryVisitor.visit_not",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_not(expr)",
        "summary": "Logical NOT over the child expression's boolean value.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:DataQueryVisitor.visit_and",
        "symbol": "DataQueryVisitor.visit_and",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_and(expr)",
        "summary": "Short-circuit AND evaluation of left and right expressions.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:DataQueryVisitor.visit_or",
        "symbol": "DataQueryVisitor.visit_or",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_or(expr)",
        "summary": "Short-circuit OR evaluation of left and right expressions.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "class:MongoQueryVisitor",
        "symbol": "MongoQueryVisitor",
        "kind": "class",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "MongoQueryVisitor(ast, case_sensitive=True)",
        "summary": "Converts a DictQuery AST into a MongoDB query dict, mapping each operator to the appropriate $-operator; validates operand types for correctness.",
        "neighbors_up": ["data/dictquery/dictquery/parsers.py", "data/dictquery/dictquery/exceptions.py"],
        "neighbors_down": ["data/dictquery/dictquery/init.py"]
    },
    {
        "id": "method:MongoQueryVisitor.evaluate",
        "symbol": "MongoQueryVisitor.evaluate",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "evaluate()",
        "summary": "Entry point: returns a MongoDB query dict for the AST. Handles bare KeyExpression (as $exists) and rejects bare VALUE expressions.",
        "neighbors_up": ["data/dictquery/dictquery/exceptions.py"],
        "neighbors_down": []
    },
    {
        "id": "method:MongoQueryVisitor._get_binary_operands",
        "symbol": "MongoQueryVisitor._get_binary_operands",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "_get_binary_operands(expr)",
        "summary": "Validate that the left side is a KeyExpression and return (left_key, right_value) via recursive visiting; raises DQEvaluationError otherwise.",
        "neighbors_up": ["data/dictquery/dictquery/parsers.py", "data/dictquery/dictquery/exceptions.py"],
        "neighbors_down": []
    },
    {
        "id": "method:MongoQueryVisitor._get_and_or_operands",
        "symbol": "MongoQueryVisitor._get_and_or_operands",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "_get_and_or_operands(expr)",
        "summary": "Normalize operands for AND/OR: convert keys to {$exists: True}; reject VALUE literals; returns (left_doc, right_doc).",
        "neighbors_up": ["data/dictquery/dictquery/parsers.py", "data/dictquery/dictquery/exceptions.py"],
        "neighbors_down": []
    },
    {
        "id": "method:MongoQueryVisitor.visit_lt",
        "symbol": "MongoQueryVisitor.visit_lt",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_lt(expr)",
        "summary": "Map '<' to {key: {'$lt': value}}.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:MongoQueryVisitor.visit_lte",
        "symbol": "MongoQueryVisitor.visit_lte",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_lte(expr)",
        "summary": "Map '<=' to {key: {'$lte': value}}.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:MongoQueryVisitor.visit_gt",
        "symbol": "MongoQueryVisitor.visit_gt",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_gt(expr)",
        "summary": "Map '>' to {key: {'$gt': value}}.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:MongoQueryVisitor.visit_gte",
        "symbol": "MongoQueryVisitor.visit_gte",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_gte(expr)",
        "summary": "Map '>=' to {key: {'$gte': value}}.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:MongoQueryVisitor.visit_equal",
        "symbol": "MongoQueryVisitor.visit_equal",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_equal(expr)",
        "summary": "Map '==' to {key: {'$eq': value}}.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:MongoQueryVisitor.visit_notequal",
        "symbol": "MongoQueryVisitor.visit_notequal",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_notequal(expr)",
        "summary": "Map '!=' to {key: {'$ne': value}}.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:MongoQueryVisitor.visit_contains",
        "symbol": "MongoQueryVisitor.visit_contains",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_contains(expr)",
        "summary": "Map CONTAINS to equality {key: {'$eq': value}} (container equality semantics for Mongo).",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:MongoQueryVisitor.visit_in",
        "symbol": "MongoQueryVisitor.visit_in",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_in(expr)",
        "summary": "Map IN to {key: {'$in': values}}.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:MongoQueryVisitor.visit_match",
        "symbol": "MongoQueryVisitor.visit_match",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_match(expr)",
        "summary": "Map MATCH to {key: {'$regex': pattern}} using compiled regex.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:MongoQueryVisitor.visit_like",
        "symbol": "MongoQueryVisitor.visit_like",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_like(expr)",
        "summary": "Translate LIKE via fnmatch.translate to a regex (respect case sensitivity) and map to $regex.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:MongoQueryVisitor.visit_key",
        "symbol": "MongoQueryVisitor.visit_key",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_key(expr)",
        "summary": "Return the raw field name for keys.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:MongoQueryVisitor.visit_number",
        "symbol": "MongoQueryVisitor.visit_number",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_number(expr)",
        "summary": "Convert numeric literal to float.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:MongoQueryVisitor.visit_boolean",
        "symbol": "MongoQueryVisitor.visit_boolean",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_boolean(expr)",
        "summary": "Return True/False from literal text.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:MongoQueryVisitor.visit_string",
        "symbol": "MongoQueryVisitor.visit_string",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_string(expr)",
        "summary": "Return string literal, optionally lowercased when case_sensitive is False.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:MongoQueryVisitor.visit_now",
        "symbol": "MongoQueryVisitor.visit_now",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_now(expr)",
        "summary": "Return current UTC datetime.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:MongoQueryVisitor.visit_none",
        "symbol": "MongoQueryVisitor.visit_none",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_none(expr)",
        "summary": "Return None.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:MongoQueryVisitor.visit_regexp",
        "symbol": "MongoQueryVisitor.visit_regexp",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_regexp(expr)",
        "summary": "Compile a regex; add re.IGNORECASE when case_sensitive is False.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:MongoQueryVisitor.visit_array",
        "symbol": "MongoQueryVisitor.visit_array",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_array(expr)",
        "summary": "Evaluate array items and return a Python list of evaluated elements.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:MongoQueryVisitor.visit_not",
        "symbol": "MongoQueryVisitor.visit_not",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_not(expr)",
        "summary": "Negate an expression in Mongo terms. Applies De Morgan for AND/OR, maps bare key to $exists: False, and otherwise wraps conditions with $not (special-casing $regex).",
        "neighbors_up": ["data/dictquery/dictquery/parsers.py"],
        "neighbors_down": []
    },
    {
        "id": "method:MongoQueryVisitor.visit_and",
        "symbol": "MongoQueryVisitor.visit_and",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_and(expr)",
        "summary": "Map to {'$and': [left_doc, right_doc]} using normalized operands.",
        "neighbors_up": [],
        "neighbors_down": []
    },
    {
        "id": "method:MongoQueryVisitor.visit_or",
        "symbol": "MongoQueryVisitor.visit_or",
        "kind": "function",
        "file_path": "data/dictquery/dictquery/visitors.py",
        "signature": "visit_or(expr)",
        "summary": "Map to {'$or': [left_doc, right_doc]} using normalized operands.",
        "neighbors_up": [],
        "neighbors_down": []
    }
]

uses = [
    {
        "id": "use:dictquery.__init__.query_to_mongo->dictquery.visitors.MongoQueryVisitor",
        "caller_file": "data/dictquery/dictquery/__init__.py",
        "callsite_snippet": "mq = MongoQueryVisitor(ast, case_sensitive)",
        "callee_symbol": "MongoQueryVisitor",
        "callee_file": "data/dictquery/dictquery/visitors.py"
    },
    {
        "id": "use:dictquery.__init__.compile->dictquery.visitors.DataQueryVisitor",
        "caller_file": "data/dictquery/dictquery/__init__.py",
        "callsite_snippet": "return DataQueryVisitor(",
        "callee_symbol": "DataQueryVisitor",
        "callee_file": "data/dictquery/dictquery/visitors.py"
    },
    {
        "id": "use:dictquery.__init__.match->dictquery.visitors.DataQueryVisitor",
        "caller_file": "data/dictquery/dictquery/__init__.py",
        "callsite_snippet": "dq = DataQueryVisitor(ast)",
        "callee_symbol": "DataQueryVisitor",
        "callee_file": "data/dictquery/dictquery/visitors.py"
    },
    {
        "id": "use:dictquery.__init__.filter->dictquery.visitors.DataQueryVisitor",
        "caller_file": "data/dictquery/dictquery/__init__.py",
        "callsite_snippet": "dq = DataQueryVisitor(",
        "callee_symbol": "DataQueryVisitor",
        "callee_file": "data/dictquery/dictquery/visitors.py"
    },
    {
        "id": "use:dictquery.__init__.query_to_mongo->dictquery.parsers.DataQueryParser",
        "caller_file": "data/dictquery/dictquery/__init__.py",
        "callsite_snippet": "ast = parser.parse(query)",
        "callee_symbol": "DataQueryParser",
        "callee_file": "data/dictquery/dictquery/parsers.py"
    },
    {
        "id": "use:dictquery.__init__.compile->dictquery.parsers.DataQueryParser",
        "caller_file": "data/dictquery/dictquery/__init__.py",
        "callsite_snippet": "ast = parser.parse(query)",
        "callee_symbol": "DataQueryParser",
        "callee_file": "data/dictquery/dictquery/parsers.py"
    },
    {
        "id": "use:dictquery.__init__.match->dictquery.parsers.DataQueryParser",
        "caller_file": "data/dictquery/dictquery/__init__.py",
        "callsite_snippet": "ast = parser.parse(query)",
        "callee_symbol": "DataQueryParser",
        "callee_file": "data/dictquery/dictquery/parsers.py"
    },
    {
        "id": "use:dictquery.__init__.filter->dictquery.parsers.DataQueryParser",
        "caller_file": "data/dictquery/dictquery/__init__.py",
        "callsite_snippet": "ast = parser.parse(query)",
        "callee_symbol": "DataQueryParser",
        "callee_file": "data/dictquery/dictquery/parsers.py"
    },
    {
        "id": "use:dictquery.datavalue.query_value->dictquery.exceptions.DQKeyError",
        "caller_file": "data/dictquery/dictquery/datavalue.py",
        "callsite_snippet": "raise DQKeyError(\"Key '{}' not found\".format(data_key))",
        "callee_symbol": "DQKeyError",
        "callee_file": "data/dictquery/dictquery/exceptions.py"
    },
    {
        "id": "use:dictquery.parsers.DataQueryParser->_exceptions.DQSyntaxError",
        "caller_file": "data/dictquery/dictquery/parsers.py",
        "callsite_snippet": "raise DQSyntaxError(\"Expected token %s\" % str(toktype))",
        "callee_symbol": "DQSyntaxError",
        "callee_file": "data/dictquery/dictquery/exceptions.py"
    },
    {
        "id": "use:dictquery.parsers.DataQueryParser->_exceptions.DQSyntaxError",
        "caller_file": "data/dictquery/dictquery/parsers.py",
        "callsite_snippet": "raise DQSyntaxError(\"Expected AND or OR instead of %s\" % self.nexttok.value)",
        "callee_symbol": "DQSyntaxError",
        "callee_file": "data/dictquery/dictquery/exceptions.py"
    },
    {
        "id": "use:dictquery.parsers.DataQueryParser->_exceptions.DQSyntaxError",
        "caller_file": "data/dictquery/dictquery/parsers.py",
        "callsite_snippet": "raise DQSyntaxError(\"Expected STRING, ARRAY, KEY\")",
        "callee_symbol": "DQSyntaxError",
        "callee_file": "data/dictquery/dictquery/exceptions.py"
    },
    {
        "id": "use:dictquery.parsers.DataQueryParser->_exceptions.DQSyntaxError",
        "caller_file": "data/dictquery/dictquery/parsers.py",
        "callsite_snippet": "raise DQSyntaxError(\"Expected binary operation %s\" % ', '.join(BINARY_OPS))",
        "callee_symbol": "DQSyntaxError",
        "callee_file": "data/dictquery/dictquery/exceptions.py"
    },
    {
        "id": "use:dictquery.parsers.DataQueryParser->_exceptions.DQSyntaxError",
        "caller_file": "data/dictquery/dictquery/parsers.py",
        "callsite_snippet": "raise DQSyntaxError(\"Can't parse expr\")",
        "callee_symbol": "DQSyntaxError",
        "callee_file": "data/dictquery/dictquery/exceptions.py"
    },
    {
        "id": "use:dictquery.parsers.DataQueryParser->dictquery.tokenizer.Token",
        "caller_file": "data/dictquery/dictquery/parsers.py",
        "callsite_snippet": "from dictquery.tokenizer import Token, gen_tokens",
        "callee_symbol": "Token",
        "callee_file": "data/dictquery/dictquery/tokenizer.py"
    },
    {
        "id": "use:dictquery.parsers.DataQueryParser->dictquery.tokenizer.gen_tokens",
        "caller_file": "data/dictquery/dictquery/parsers.py",
        "callsite_snippet": "self.tokens = gen_tokens(query)",
        "callee_symbol": "gen_tokens",
        "callee_file": "data/dictquery/dictquery/tokenizer.py"
    },
    {
        "id": "use:dictquery.tokenizer.gen_tokens->dictquery.exceptions.DQSyntaxError",
        "caller_file": "data/dictquery/dictquery/tokenizer.py",
        "callsite_snippet": "raise DQSyntaxError(\"Unexpected character at pos %d\" % match.start())",
        "callee_symbol": "DQSyntaxError",
        "callee_file": "data/dictquery/dictquery/exceptions.py"
    },
    {
        "id": "use:dictquery.visitors.DataQueryVisitor->dictquery.exceptions.DQException",
        "caller_file": "data/dictquery/dictquery/visitors.py",
        "callsite_snippet": "raise DQException('self.data is not specified')",
        "callee_symbol": "DQException",
        "callee_file": "data/dictquery/dictquery/exceptions.py"
    },
    {
        "id": "use:dictquery.visitors.DataQueryVisitor->dictquery.datavalue.query_value",
        "caller_file": "data/dictquery/dictquery/visitors.py",
        "callsite_snippet": "return query_value(\n            self.data, dict_key, self.use_nested_keys,",
        "callee_symbol": "query_value",
        "callee_file": "data/dictquery/dictquery/datavalue.py"
    },
    {
        "id": "use:dictquery.visitors.DataQueryVisitor->dictquery.datavalue.DataQueryItem",
        "caller_file": "data/dictquery/dictquery/visitors.py",
        "callsite_snippet": "return DataQueryItem(\n            key=expr.value,",
        "callee_symbol": "DataQueryItem",
        "callee_file": "data/dictquery/dictquery/datavalue.py"
    },
    {
        "id": "use:dictquery.visitors.MongoQueryVisitor->dictquery.exceptions.DQEvaluationError",
        "caller_file": "data/dictquery/dictquery/visitors.py",
        "callsite_snippet": "raise DQEvaluationError(\"Left operand must be `KeyExpression`\")",
        "callee_symbol": "DQEvaluationError",
        "callee_file": "data/dictquery/dictquery/exceptions.py"
    },
    {
        "id": "use:dictquery.visitors.MongoQueryVisitor->dictquery.exceptions.DQEvaluationError",
        "caller_file": "data/dictquery/dictquery/visitors.py",
        "callsite_snippet": "raise DQEvaluationError(\"For `AND`, `OR` operands must be expression or key\")",
        "callee_symbol": "DQEvaluationError",
        "callee_file": "data/dictquery/dictquery/exceptions.py"
    },
    {
        "id": "use:dictquery.visitors.MongoQueryVisitor->dictquery.exceptions.DQEvaluationError",
        "caller_file": "data/dictquery/dictquery/visitors.py",
        "callsite_snippet": "raise DQEvaluationError(\"Expected expression or key\")",
        "callee_symbol": "DQEvaluationError",
        "callee_file": "data/dictquery/dictquery/exceptions.py"
    },
    {
        "id": "use:dictquery.visitors.MongoQueryVisitor->dictquery.parsers.KeyExpression",
        "caller_file": "data/dictquery/dictquery/visitors.py",
        "callsite_snippet": "if not isinstance(expr.left, KeyExpression):",
        "callee_symbol": "KeyExpression",
        "callee_file": "data/dictquery/dictquery/parsers.py"
    },
    {
        "id": "use:dictquery.visitors.MongoQueryVisitor->dictquery.parsers.VALUE_EXPRESSIONS",
        "caller_file": "data/dictquery/dictquery/visitors.py",
        "callsite_snippet": "if isinstance(expr.left, VALUE_EXPRESSIONS) or isinstance(expr.right, VALUE_EXPRESSIONS):",
        "callee_symbol": "VALUE_EXPRESSIONS",
        "callee_file": "data/dictquery/dictquery/parsers.py"
    },
    {
        "id": "use:dictquery.visitors.MongoQueryVisitor->dictquery.parsers.AndExpression",
        "caller_file": "data/dictquery/dictquery/visitors.py",
        "callsite_snippet": "if isinstance(expr.value, AndExpression):",
        "callee_symbol": "AndExpression",
        "callee_file": "data/dictquery/dictquery/parsers.py"
    },
    {
        "id": "use:dictquery.visitors.MongoQueryVisitor->dictquery.parsers.OrExpression",
        "caller_file": "data/dictquery/dictquery/visitors.py",
        "callsite_snippet": "elif isinstance(expr.value, OrExpression):",
        "callee_symbol": "OrExpression",
        "callee_file": "data/dictquery/dictquery/parsers.py"
    },
    {
        "id": "use:dictquery.visitors.MongoQueryVisitor->dictquery.parsers.NotExpression",
        "caller_file": "data/dictquery/dictquery/visitors.py",
        "callsite_snippet": "left=NotExpression(expr.value.left),",
        "callee_symbol": "NotExpression",
        "callee_file": "data/dictquery/dictquery/parsers.py"
    }
]

eps = [
    {
        "id": "ep:module:dictquery.__init__",
        "type": "module",
        "file_path": "data/dictquery/dictquery/__init__.py",
        "handler": "__init__",
        "downstream_files": [
            "data/dictquery/dictquery/parsers.py",
            "data/dictquery/dictquery/visitors.py"
        ]
    }
]