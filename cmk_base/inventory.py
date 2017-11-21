#!/usr/bin/python
# -*- encoding: utf-8; py-indent-offset: 4 -*-
# +------------------------------------------------------------------+
# |             ____ _               _        __  __ _  __           |
# |            / ___| |__   ___  ___| | __   |  \/  | |/ /           |
# |           | |   | '_ \ / _ \/ __| |/ /   | |\/| | ' /            |
# |           | |___| | | |  __/ (__|   <    | |  | | . \            |
# |            \____|_| |_|\___|\___|_|\_\___|_|  |_|_|\_\           |
# |                                                                  |
# | Copyright Mathias Kettner 2014             mk@mathias-kettner.de |
# +------------------------------------------------------------------+
#
# This file is part of Check_MK.
# The official homepage is at http://mathias-kettner.de/check_mk.
#
# check_mk is free software;  you can redistribute it and/or modify it
# under the  terms of the  GNU General Public License  as published by
# the Free Software Foundation in version 2.  check_mk is  distributed
# in the hope that it will be useful, but WITHOUT ANY WARRANTY;  with-
# out even the implied warranty of  MERCHANTABILITY  or  FITNESS FOR A
# PARTICULAR PURPOSE. See the  GNU General Public License for more de-
# tails. You should have  received  a copy of the  GNU  General Public
# License along with GNU Make; see the file  COPYING.  If  not,  write
# to the Free Software Foundation, Inc., 51 Franklin St,  Fifth Floor,
# Boston, MA 02110-1301 USA.

"""Currently this module manages the inventory tree which is built
while the inventory is performed for one host.

In the future all inventory code should be moved to this module."""

import gzip
import inspect
import os
import pprint
import re
import sys

import cmk
import cmk.paths
import cmk.tty as tty
import cmk.defines as defines
from cmk.exceptions import MKGeneralException

import cmk_base.utils
import cmk_base.console as console
import cmk_base.config as config
import cmk_base.rulesets as rulesets
import cmk_base.checks as checks
import cmk_base.check_api as check_api
import cmk_base.snmp as snmp
import cmk_base.discovery as discovery
import cmk_base.ip_lookup as ip_lookup
import cmk_base.data_sources as data_sources

inventory_output_dir  = cmk.paths.var_dir + "/inventory"
inventory_archive_dir = cmk.paths.var_dir + "/inventory_archive"
# TODO: This is not configurable. Drop the flag?
inventory_pprint_output = True

_inv_hw_changes  = 0
_inv_sw_changes  = 0
_inv_sw_missing  = 0
_inv_fail_status = 1 # State in case of an error (default: WARN)

#.
#   .--Inventory-----------------------------------------------------------.
#   |            ___                      _                                |
#   |           |_ _|_ ____   _____ _ __ | |_ ___  _ __ _   _              |
#   |            | || '_ \ \ / / _ \ '_ \| __/ _ \| '__| | | |             |
#   |            | || | | \ V /  __/ | | | || (_) | |  | |_| |             |
#   |           |___|_| |_|\_/ \___|_| |_|\__\___/|_|   \__, |             |
#   |                                                   |___/              |
#   +----------------------------------------------------------------------+
#   | Code for doing the actual inventory                                  |
#   '----------------------------------------------------------------------'

def do_inv(hostnames):
    _ensure_directory(inventory_output_dir)
    _ensure_directory(inventory_archive_dir)

    errors = []
    for hostname in hostnames:
        try:
            console.verbose("Doing HW/SW-Inventory for %s..." % hostname)

            if config.is_cluster(hostname):
                ipaddress = None
            else:
                ipaddress = ip_lookup.lookup_ip_address(hostname)

            _do_inv_for(hostname, ipaddress)
            console.verbose("..OK\n")
        except Exception, e:
            if cmk.debug.enabled():
                raise

            console.verbose("Failed: %s\n" % e)
            errors.append("Failed to inventorize %s: %s" % (hostname, e))
        finally:
            for data_source, exceptions in data_sources.get_data_source_errors_of_host(hostname, ipaddress).items():
                for exc in exceptions:
                    errors.append("%s" % exc)

            cmk_base.utils.cleanup_globals()

    if errors:
        raise MKGeneralException("\n".join(errors))


def do_inv_check(options, hostname):
    global _inv_hw_changes, _inv_sw_changes, _inv_sw_missing, _inv_fail_status
    _inv_hw_changes  = options.get("hw-changes", _inv_hw_changes)
    _inv_sw_changes  = options.get("sw-changes", _inv_sw_changes)
    _inv_sw_missing  = options.get("sw-missing", _inv_sw_missing)
    _inv_fail_status = options.get("inv-fail-status", _inv_fail_status)

    try:
        if config.is_cluster(hostname):
            ipaddress = None
        else:
            ipaddress = ip_lookup.lookup_ip_address(hostname)

        inv_tree, old_timestamp = _do_inv_for(hostname, ipaddress)
        num_entries = _count_nodes(g_inv_tree)
        if not num_entries:
            console.output("OK - Found no data\n")
            return 0

        infotexts = []

        infotexts.append("found %d entries" % num_entries)
        state = 0
        if not inv_tree.get("software") and _inv_sw_missing:
            infotexts.append("software information is missing" + check_api.state_markers[_inv_sw_missing])
            state = _inv_sw_missing

        if old_timestamp:
            # TODO: Use cmk.store
            path = inventory_archive_dir + "/" + hostname + "/%d" % old_timestamp
            old_tree = eval(file(path).read())

            if inv_tree.get("software") != old_tree.get("software"):
                infotext = "software changes"
                if _inv_sw_changes:
                    state = _inv_sw_changes
                    infotext += check_api.state_markers[_inv_sw_changes]
                infotexts.append(infotext)

            if inv_tree.get("hardware") != old_tree.get("hardware"):
                infotext = "hardware changes"
                if state == 2 or _inv_hw_changes == 2:
                    state = 2
                else:
                    state = max(state, _inv_sw_changes)
                if _inv_hw_changes:
                    infotext += check_api.state_markers[_inv_hw_changes]

                infotexts.append(infotext)

        for data_source, exceptions in data_sources.get_data_source_errors_of_host(hostname, ipaddress).items():
            for exc in exceptions:
                infotexts.append("%s" % exc)

        console.output("%s - %s\n" % (defines.short_service_state_name(state), ", ".join(infotexts)))
        return state

    except Exception, e:
        if cmk.debug.enabled():
            raise
        console.output("%s - Inventory failed: %s\n" %
            (defines.short_service_state_name(_inv_fail_status), e))
        return _inv_fail_status


def _do_inv_for(hostname, ipaddress):
    _initialize_inventory_tree()

    node = inv_tree("software.applications.check_mk.cluster.")

    if config.is_cluster(hostname):
        node["is_cluster"] = True
        _do_inv_for_cluster(hostname)
    else:
        node["is_cluster"] = False
        _do_inv_for_realhost(hostname, ipaddress)

    # Remove empty paths
    _cleanup_inventory_tree(g_inv_tree)
    old_timestamp = _save_inventory_tree(hostname)

    console.verbose("..%s%s%d%s entries" %
            (tty.bold, tty.yellow, _count_nodes(g_inv_tree), tty.normal))

    _run_inventory_export_hooks(hostname)
    return _get_inventory_tree(), old_timestamp


def _do_inv_for_cluster(hostname):
    inv_node = inv_tree_list("software.applications.check_mk.cluster.nodes:")
    for node_name in config.nodes_of(hostname):
        inv_node.append({
            "name" : node_name,
        })


def _do_inv_for_realhost(hostname, ipaddress):
    sources = data_sources.DataSources(hostname)

    for source in sources.get_data_sources():
        if isinstance(source, data_sources.SNMPDataSource):
            source.set_on_error("raise")
            source.set_do_snmp_scan(True)
            source.set_use_snmpwalk_cache(False)
            source.set_ignore_check_interval(True)
            source.set_check_plugin_name_filter(_gather_snmp_check_plugin_names_inventory)

    multi_host_sections = sources.get_host_sections(hostname, ipaddress)

    import cmk_base.inventory_plugins
    for section_name, plugin in cmk_base.inventory_plugins.inv_info.items():
        section_content = multi_host_sections.get_section_content(hostname, ipaddress,
                                                                  section_name, for_discovery=False)

        if section_content is None: # No data for this check type
            continue

        # TODO: Don't we need to take checks.check_info[check_plugin_name]["handle_empty_info"]:
        #       like it is done in checking.execute_check()? Standardize this!
        if not section_content: # section not present (None or [])
            # Note: this also excludes existing sections without info..
            continue

        console.verbose(tty.green + tty.bold + section_name + " " + tty.normal)

        # Inventory functions can optionally have a second argument: parameters.
        # These are configured via rule sets (much like check parameters).
        inv_function = plugin["inv_function"]
        if len(inspect.getargspec(inv_function).args) == 2:
            params = _get_inv_params(hostname, section_name)
            inv_function(section_content, params)
        else:
            inv_function(section_content)


def _gather_snmp_check_plugin_names_inventory(hostname, ipaddress, on_error, do_snmp_scan):
    return discovery.gather_snmp_check_plugin_names(hostname, ipaddress, on_error, do_snmp_scan, for_inventory=True)


def _get_inv_params(hostname, section_name):
    return rulesets.host_extra_conf_merged(hostname, config.inv_parameters.get(section_name, []))


# Creates the directory at path if it does not exist.  If that path does exist
# it is assumed that it is a directory. the file type is not being checked.
# This function is atomar so that no exception can arise if two processes
# at the same time try to create the directory. Only fails if the directory
# is not present for any reason after this function call.
# TODO: Is not called often. Should we make this available in a general place
# and use it more often or drop it?
def _ensure_directory(path):
    try:
        os.makedirs(path)
    except Exception:
        if os.path.exists(path):
            return
        raise

#.
#   .--Inventory Tree------------------------------------------------------.
#   |  ___                      _                     _____                |
#   | |_ _|_ ____   _____ _ __ | |_ ___  _ __ _   _  |_   _| __ ___  ___   |
#   |  | || '_ \ \ / / _ \ '_ \| __/ _ \| '__| | | |   | || '__/ _ \/ _ \  |
#   |  | || | | \ V /  __/ | | | || (_) | |  | |_| |   | || | |  __/  __/  |
#   | |___|_| |_|\_/ \___|_| |_|\__\___/|_|   \__, |   |_||_|  \___|\___|  |
#   |                                         |___/                        |
#   +----------------------------------------------------------------------+
#   | Managing the inventory tree of a host                                |
#   '----------------------------------------------------------------------'

g_inv_tree = {}

def _initialize_inventory_tree():
    global g_inv_tree
    g_inv_tree = {}


def _get_inventory_tree():
    return g_inv_tree


# This is just a small wrapper for the inv_tree() function which makes
# it clear that the requested tree node is treated as a list.
def inv_tree_list(path):
    # The [] is needed to tell pylint that a list is returned
    return inv_tree(path, [])


# Function for accessing the inventory tree of the current host
# Example: path = "software.packages:17."
# Function for accessing the inventory tree of the current host
# Example: path = "software.packages:17."
# The path must end with : or .
# -> software is a dict
# -> packages is a list
def inv_tree(path, default_value=None):
    if default_value != None:
        node = default_value
    else:
        node = {}

    current_what = "."
    current_path = ""

    while path:
        if current_path == "":
            node = g_inv_tree

        parts = re.split("[:.]", path)
        name = parts[0]
        what = path[len(name)]
        path = path[1 + len(name):]
        current_path += what + name

        if current_what == '.': # node is a dict
            if name not in node:
                if what == '.':
                    node[name] = {}
                else:
                    node[name] = []
            node = node[name]

        else: # node is a list
            try:
                index = int(name)
            except:
                raise MKGeneralException("Cannot convert index %s of path %s into int" % (name, current_path))

            if type(node) != list:
                raise MKGeneralException("Path %s is exptected to by of type list, but is dict" % current_path)

            if index < 0 or index >= len(node):
                raise MKGeneralException("Index %d not existing in list node %s" % (index, current_path))
            node = node[index]

        current_what = what

    return node


# Removes empty nodes from a (sub)-tree. Returns
# True if the tree itself is empty
def _cleanup_inventory_tree(tree):
    if type(tree) == dict:
        for key, value in tree.items():
            if _cleanup_inventory_tree(value):
                del tree[key]
        return not tree

    elif type(tree) == list:
        to_delete = []
        for nr, entry in enumerate(tree):
            if _cleanup_inventory_tree(entry):
                to_delete.append(nr)
        for nr in to_delete[::-1]:
            del tree[nr]
        return not tree

    else:
        return False # cannot clean non-container nodes


def _count_nodes(tree):
    if type(tree) == dict:
        return len(tree) + sum([_count_nodes(v) for v in tree.values()])
    elif type(tree) == list:
        return len(tree) + sum([_count_nodes(v) for v in tree])
    elif tree == None:
        return 0
    else:
        return 1


# Returns the time stamp of the previous inventory with different
# outcome or None.
def _save_inventory_tree(hostname):
    if not os.path.exists(inventory_output_dir):
        os.makedirs(inventory_output_dir)

    old_time = None

    if inventory_pprint_output:
        r = pprint.pformat(g_inv_tree)
    else:
        r = repr(g_inv_tree)

    path = inventory_output_dir + "/" + hostname
    if g_inv_tree:
        old_tree = None
        if os.path.exists(path):
            try:
                old_tree = eval(file(path).read())
            except:
                pass

        if old_tree != g_inv_tree:
            if old_tree:
                console.verbose("..changed")
                old_time = os.stat(path).st_mtime
                arcdir = "%s/%s" % (inventory_archive_dir, hostname)
                if not os.path.exists(arcdir):
                    os.makedirs(arcdir)
                os.rename(path, arcdir + ("/%d" % old_time))
            else:
                console.verbose("..new")

            file(path, "w").write(r + "\n")
            gzip.open(path + ".gz", "w").write(r + "\n")
            # Inform Livestatus about the latest inventory update
            file(inventory_output_dir + "/.last", "w")
        else:
            console.verbose("..unchanged")

    else:
        if os.path.exists(path): # Remove empty inventory files. Important for host inventory icon
            os.remove(path)
        if os.path.exists(path + ".gz"):
            os.remove(path + ".gz")

    return old_time


def _run_inventory_export_hooks(hostname):
    import cmk_base.inventory_plugins
    for hookname, ruleset in config.inv_exports.items():
        entries = rulesets.host_extra_conf(hostname, ruleset)
        if entries:
            console.verbose(", running %s%s%s%s..." % (tty.blue, tty.bold, hookname, tty.normal))
            params = entries[0]
            try:
                cmk_base.inventory_plugins.inv_export[hookname]["export_function"](hostname, params, g_inv_tree)
            except Exception, e:
                if cmk.debug.enabled():
                    raise
                raise MKGeneralException("Failed to execute export hook %s: %s" % (
                    hookname, e))
