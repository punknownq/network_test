# -*- coding: utf-8 -*-
import sys, socket, json, time
from select import select
from collections import defaultdict, namedtuple
from threading import Thread, Timer
from datetime import datetime
from copy import deepcopy
import prettytable as pt

SIZE = 4096

# user commands and inter-node protocol update types
# note: set of commands and set of protocol messages intersect!
#       if you want a list see user_cmds and udpates near main.
LINKDOWN      = "linkdown"
LINKUP        = "linkup"
LINKCHANGE    = "linkchange"
SHOWRT        = "showrt"
CLOSE         = "close"
COSTSUPDATE   = "costsupdate"
SHOWNEIGHBORS = "neighbors"
NODES         = "nodes"


class RepeatTimer(Thread):
    """
    thread that will call a function every interval seconds
    """
    def __init__(self, interval, target):
        Thread.__init__(self)
        self.target = target
        self.interval = interval
        self.daemon = True
        self.stopped = False
    def run(self):
        while not self.stopped:
            time.sleep(self.interval)
            self.target()


class ResettableTimer():

    """
    ensure neighbor is transmitting cost updates using a resettable timer
    """
    def __init__(self, interval, func, args=None):
        if args != None: assert type(args) is list
        self.interval = interval
        self.func = func
        self.args = args
        self.countdown = self.create_timer()
    def start(self):
        self.countdown.start()
    def reset(self):
        self.countdown.cancel()
        self.countdown = self.create_timer()
        self.start()
    def create_timer(self):
        t = Timer(self.interval, self.func, self.args)
        t.daemon = True
        return t
    def cancel(self):
        self.countdown.cancel()



def estimate_costs():
    """ recalculate inter-node path costs using bellman ford algorithm """
    for destination_addr, destination in nodes.items():
        # we don't need to update the distance to ourselves
        if destination_addr != me:
            # iterate through neighbors and find cheapest route
            cost = float("inf")
            nexthop = ''
            for neighbor_addr, neighbor in get_neighbors().items():
                # distance = direct cost to neighbor + cost from neighbor to destination
                if destination_addr in neighbor['costs']:
                    dist = neighbor['direct'] + neighbor['costs'][destination_addr]
                    if dist < cost:
                        cost = dist
                        if cost > 15:
                            cost = float("inf")
                        nexthop = neighbor_addr
            # set new estimated cost to node in the network
            destination['cost'] = cost
            destination['route'] = nexthop


def update_costs(host, port, **kwargs):
    """ update neighbor's costs """
    costs = kwargs['costs']
    addr = addr2key(host, port) # 这个addr是sender的addr，不是表中所有的addr
    # if a node listed in costs is not in our list of nodes...
    for node in costs:
        if node not in nodes:
            # ... create a new node
            nodes[node] = default_node()
    # if node not a neighbor ...
    if not nodes[addr]['is_neighbor']: # 这里是对sender进行判断，也就是接受者的neighbor
        # ... make it your neighbor!
        print("making new neighbor {0}\n".format(addr))
        del nodes[addr]
        nodes[addr] = create_node(
                cost        = nodes[addr]['cost'],
                is_neighbor = True,
                direct      = kwargs['neighbor']['direct'],
                costs       = costs,
                addr        = addr)
    else:
        # otherwise just update node costs，更新这个邻接点的costs表单
        node = nodes[addr]
        node['costs'] = costs
        # restart silence monitor
        node['silence_monitor'].reset()
    # run bellman ford
    estimate_costs()


def broadcast_costs():
    """ send estimated path costs to each neighbor """
    costs = { addr: node['cost'] for addr, node in nodes.items() }
    data = { 'type': COSTSUPDATE }  # 这个会触发updata_costs的函数
    for neighbor_addr, neighbor in get_neighbors().items():
        # poison reverse
        poisoned_costs = deepcopy(costs)
        for dest_addr, cost in costs.items():
            # only do poisoned reverse if destination not me or neighbor
            if dest_addr not in [me, neighbor_addr]:
                # if we route through neighbor to get to destination ...
                if nodes[dest_addr]['route'] == neighbor_addr:
                    # ... tell neighbor distance to destination is infinty!
                    poisoned_costs[dest_addr] = float("inf")
        data['payload'] = { 'costs': poisoned_costs }
        data['payload']['neighbor'] = { 'direct': neighbor['direct'] }
        # send (potentially 'poisoned') costs to neighbor
        sock.sendto(json.dumps(data).encode(), key2addr(neighbor_addr))

def setup_server(host, port):
    """ setup a UDP server"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((host, port))
        print("listening on {0}:{1}\n".format(host, port))
    except socket.error:
        print("an error occured binding the server socket.error ")
        sys.exit(1)
    return sock

def default_node():
    """default node type"""
    return { 'cost': float("inf"), 'is_neighbor': False, 'route': '' }


def create_node(cost, is_neighbor, direct=None, costs=None, addr=None):
    """ centralizes the pattern for creating new nodes """
    node = default_node()
    node['cost'] = cost
    node['is_neighbor'] = is_neighbor
    node['direct'] = direct if direct != None else float("inf")
    node['costs']  = costs  if costs  != None else defaultdict(lambda: float("inf"))
    if is_neighbor:
        node['route'] = addr
        # ensure neighbor is transmitting cost updates using a resettable timer
        monitor = ResettableTimer(
            interval = 3*run_args.timeout,
            func = linkdown,
            args = list(key2addr(addr)))
        monitor.start()
        node['silence_monitor'] = monitor
    return node


def get_node(host, port):
    """ returns formatted address and node info for that addr """
    error = False
    addr = addr2key(get_host(host), port)
    if not in_network(addr):
        error = 'node not in network'
    node = nodes[addr]
    return node, addr, error


def linkchange(host, port, **kwargs):
    """ change the link between the nodes """
    node, addr, err = get_node(host, port)
    if err: return
    if not node['is_neighbor']:
        print("node {0} is not a neighbor so the link cost can't be changed\n".format(addr))
        return
    direct = kwargs['direct']
    if direct < 1:
        print("the minimum amount a link cost between nodes can be is 1")
        return
    if 'saved' in node:
        print("this link currently down. please first bring link back to life using LINKUP cmd.")
        return
    node['direct'] = direct
    # run bellman-ford
    estimate_costs()


def linkdown(host, port, **kwargs):
    """ take down the link between the nodes """
    node, addr, err = get_node(host, port)
    if err: return
    if not node['is_neighbor']:
        print("node {0} is not a neighbor so it can't be taken down\n".format(addr))
        return
    # save direct distance to neighbor, then set to infinity
    node['saved'] = node['direct']
    node['direct'] = float("inf")
    node['is_neighbor'] = False
    node['silence_monitor'].cancel()
    # run bellman-ford
    estimate_costs()


def linkup(host, port, **kwargs):
    """ recover the link bwtween the nodes """
    node, addr, err = get_node(host, port)
    if err: return
    # make sure node was previously taken down via LINKDOWN cmd
    if 'saved' not in node:
        print("{0} wasn't a previous neighbor\n".format(addr))
        return
    # restore saved direct distance
    node['direct'] = node['saved']
    del node['saved']
    node['is_neighbor'] = True
    # run bellman-ford
    estimate_costs()


def formatted_now():
    """return format time"""
    return datetime.now().strftime("%b-%d-%Y, %I:%M %p, %S seconds")


def show_neighbors():
    """ show active neighbors """
    print(formatted_now())
    print("Neighbors: ")
    for addr, neighbor in get_neighbors().items():
        print("{addr}, cost:{cost}, direct:{direct}".format(
                addr   = addr,
                cost   = neighbor['cost'],
                direct = neighbor['direct']))
    # print # extra line


def get_neighbors_num():
    """get active neighbors num"""
    neighbors_num = 0
    for _, _ in get_neighbors().items():
        neighbors_num += 1
    return neighbors_num


def showrt():
    """ display routing info: cost to destination; route to take """
    print(formatted_now())
    print("Distance vector list is:")
    # for addr, node in nodes.items():
    #     if addr != me:
    #         print ("Destination = {destination}, "
    #                "Cost = {cost}, "
    #                "Link = ({nexthop})").format(
    #                     destination = addr,
    #                     cost        = node['cost'],
    #                     nexthop     = node['route'])
    # print # extra line
    tb = pt.PrettyTable()
    tb.field_names = ["destination", "nexthop", "cost"]
    for addr, node in nodes.items():
        if addr != me:
            tb.add_row([node_name[addr[-5:]], node_name[node['route'][-5:]], node['cost']])
            # tb.add_row([addr[-5:], node['route'][-5:], node['cost']])
    print(tb)


def close():
    """ notify all neighbors that she's a comin daaaahwn! then close process"""
    sys.exit()


def in_network(addr):
    if addr not in nodes:
        print('node {0} is not in the network\n'.format(addr))
        return False
    return True


def key2addr(key):
    """key to addr"""
    host, port = key.split(':')
    return host, int(port)


def addr2key(host, port):
    """addr to key"""
    return "{host}:{port}".format(host=host, port=port)


def get_host(host):
    """ translate host into ip address """
    return localhost if host == 'localhost' else host


def get_neighbors():
    """ return dict of all neighbors (does not include self) """
    return dict([d for d in nodes.items() if d[1]['is_neighbor']])


def is_number(n):
    """Determine if it is a number"""
    try:
        float(n)
        return True
    except ValueError:
        return False


def is_int(i):
    """Determine if it is a int-type data"""
    try:
        int(i)
        return True
    except ValueError:
        return False

def parse_argv():
    """
    pythonicize bflient run args(first run)
    """
    s = sys.argv[1:]
    parsed = {}
    # validate port
    port = s.pop(0)
    if not is_int(port):
        return { 'error': "port values must be integers. {0} is not an int.".format(port) }
    parsed['port'] = int(port)
    # validate timeout
    timeout = s.pop(0)
    if not is_number(timeout):
        return { 'error': "timeout must be a number. {0} is not a number.".format(timeout) }
    parsed['timeout'] = float(timeout)
    # iterate through s extracting and validating neighbors and costs along the way
    parsed['neighbors'] = []
    parsed['costs'] = []
    while len(s):
        if len(s) < 3:
            return { 'error': "please provide host, port, and link cost for each link." }
        host = get_host(s[0].lower())
        port = s[1]
        if not is_int(port):
            return { 'error': "port values must be integers. {0} is not an int.".format(port) }
        parsed['neighbors'].append(addr2key(host, port))
        cost = s[2]
        if not is_number(cost):
            return { 'error': "link costs must be numbers. {0} is not a number.".format(cost) }
        parsed['costs'].append(float(s[2]))
        del s[0:3]
    return parsed

def parse_user_input(user_input):
    """
    validate user input and parse values into dict. returns (error, parsed) tuple.(when program is running)
    (note: yes, I know I should be raising exceptions instead of returning {'err'} dicts)
    """
    # define default return value
    parsed = { 'addr': (), 'payload': {} }
    user_input = user_input.split()
    if not len(user_input):
        return { 'error': "please provide a command\n" }
    cmd = user_input[0].lower()
    # verify cmd is valid
    if cmd not in user_cmds:
        return { 'error': "'{0}' is not a valid command\n".format(cmd) }
    # cmds below require args
    if cmd in [LINKDOWN, LINKUP, LINKCHANGE]:
        args = user_input[1:]
        # validate args
        if cmd in [LINKDOWN, LINKUP] and len(args) != 2:
            return { 'error': "'{0}' cmd requires args: host, port\n".format(cmd) }
        elif cmd == LINKCHANGE and len(args) != 3:
            return { 'error': "'{0}' cmd requires args: host, port, link cost\n".format(cmd) }
        port = args[1]
        if not is_int(port):
            return { 'error': "port must be an integer value\n" }
        parsed['addr'] = (get_host(args[0]), int(port))
        if cmd == LINKCHANGE:
            cost = args[2]
            if not is_number(cost):
                return { 'error': "new link weight must be a number\n" }
            parsed['payload'] = { 'direct': float(cost) }
    parsed['cmd'] = cmd
    return parsed

def print_nodes():
    """ helper function for debugging """
    print("nodes: ")
    for addr, node in nodes.items():
        print(addr)
        for k,v in node.items():
            print('---- ', k, '\t\t', v)
    #print # extra line

# map command/update/node_name names to functions
user_cmds = {
    LINKDOWN   : linkdown,
    LINKUP     : linkup,
    LINKCHANGE : linkchange,
    SHOWRT     : showrt,
    CLOSE      : close,
    SHOWNEIGHBORS : show_neighbors,
    NODES      : print_nodes,
}
updates = {
    LINKDOWN   : linkdown,
    LINKUP     : linkup,
    LINKCHANGE : linkchange,
    COSTSUPDATE: update_costs,
}
node_name = {
    '20000' : "A",
    '20001' : "B",
    '20002' : "C",
    '20003' : "D",
    '20004' : "E",
}

if __name__ == '__main__':
    localhost = socket.gethostbyname(socket.gethostname())
    parsed = parse_argv()
    if 'error' in parsed:
        print(parsed['error'])
        sys.exit(1)
    RunArgs = namedtuple('RunInfo', 'port timeout neighbors costs')
    run_args = RunArgs(**parsed)
    # initialize dict of nodes to all neighbors
    nodes = defaultdict(lambda: default_node())
    for neighbor, cost in zip(run_args.neighbors, run_args.costs):
        nodes[neighbor] = create_node(
                cost=cost, direct=cost, is_neighbor=True, addr=neighbor)
    # begin accepting UDP packets
    sock = setup_server(localhost, run_args.port)
    # set cost to myself to 0
    me = addr2key(*sock.getsockname())
    nodes[me] = create_node(cost=0.0, direct=0.0, is_neighbor=False, addr=me)
    # for print the log
    neighbors_num = get_neighbors_num()
    i = 0
    iter_num = 0
    # broadcast costs every timeout seconds
    broadcast_costs()
    RepeatTimer(run_args.timeout, broadcast_costs).start()

    # listen for updates from other nodes and user input
    inputs = [sock, sys.stdin]
    running = True
    while running:
        in_ready, out_ready, except_ready = select(inputs,[],[])
        for s in in_ready:
            if s == sys.stdin:
                # user input command
                parsed = parse_user_input(sys.stdin.readline())
                if 'error' in parsed:
                    print(parsed['error'])
                    continue
                cmd = parsed['cmd']
                if cmd in [LINKDOWN, LINKUP, LINKCHANGE]:
                    # notify node on other end of the link of action
                    data = json.dumps({ 'type': cmd, 'payload': parsed['payload'] })
                    sock.sendto(data, parsed['addr'])
                # perform cmd on this side of the link
                user_cmds[cmd](*parsed['addr'], **parsed['payload'])
            else:
                # update from another node
                i += 1
                data, sender = s.recvfrom(SIZE)
                loaded = json.loads(data)
                update = loaded['type']
                payload = loaded['payload']
                if update not in updates:
                    print("'{0}' is not in the update protocol\n".format(update))
                    continue
                updates[update](*sender, **payload)
                # show the result when collect all the message from the neighbors
                if i % neighbors_num == 0:
                    showrt()
                    i = 0
    sock.close()
