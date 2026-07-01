from vm.cli import build_parser


def test_parser_has_layer1_commands():
    parser = build_parser()
    args = parser.parse_args(["list", "--provider", "lambda", "--json"])
    assert args.command == "list" and args.provider == "lambda" and args.json is True


def test_create_requires_provider_and_gpu():
    parser = build_parser()
    ns = parser.parse_args(["create", "--provider", "crusoe", "--gpu", "h100.8x", "--count", "2"])
    assert ns.count == 2 and ns.gpu == "h100.8x"


def test_fleet_create_parses():
    ns = build_parser().parse_args(["fleet", "create", "--gpu", "h100.8x", "--count", "4", "--name", "f"])
    assert ns.fleet_command == "create" and ns.count == 4
