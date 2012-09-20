# pypickupbot - An ircbot that helps game players to play organized games
#               with captain-picked teams.
#     Copyright (C) 2010 pypickupbot authors
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

"""Pickup-related commands"""
from time import time
from datetime import datetime
import random
import json
import httplib

from twisted.python import log

from pypickupbot.modable import SimpleModuleFactory
from pypickupbot.irc import COMMAND, InputError
from pypickupbot.topic import Topic
from pypickupbot import db
from pypickupbot import config
from pypickupbot.misc import str_from_timediff, timediff_from_str,\
    InvalidTimeDiffString, StringTypes, itime
    
class Player:

    def __init__(self, nick, playerid = None, create_dt = None):
        self.nick           = nick
        self.playerid       = playerid
        self.create_dt      = create_dt
        self.player_info    = None

    def __str__(self):
        return "{0} (#{1})".format(self.nick, self.playerid)

    def get_xonstat_url(self):
        """return xontat url for specific player"""
        return config.get("Xonstat Interface", "url").decode('string-escape') + "player/" + str(self.playerid)

    def get_id(self):
        return self.playerid

    def _get_xonstat_json(self, request):
        server = config.get("Xonstat Interface", "server").decode('string-escape')
        try:
            http = httplib.HTTPConnection(server)
            http.connect()
            http.request("GET", request)
            response = http.getresponse()
            http.close()
        except:
            return None
        try:
            json_data = json.loads(response.read())[0]  # dict embedded in a list
        except:
            json_data = {}
        return json_data

    def _get_player_info(self):
        if not self.player_info:
            self.player_info = self._get_xonstat_json("/player/{0}.json".format(self.playerid))
        return self.player_info

    def get_nick(self):
        try:
            return self._get_player_info()['player']['stripped_nick']
        except:
            return self.nick

    def get_elo_dict(self):
        return self._get_player_info()['elos']

    def get_rank_dict(self):
        return self._get_player_info()['ranks']

    def get_elo(self, gametype):
        gt = gametype.lower()
        try:
            elos = self._get_player_info()['elos']
            if elos.has_key(gt):
                return elos[gt]['elo']
        except:
            pass
        return 0

    def get_rank(self, gametype):
        gt = gametype.lower()
        try:
            ranks = self._get_player_info()['ranks']
            if ranks.has_key(gt):
                return (ranks[gt]['rank'], ranks[gt]['max_rank'])
        except:
            pass
        return (None, None)


class Team:

    def __init__(self, name, gametype):
        self.players    = []
        self.name       = name
        self.gametype   = None
        self.elo        = 0
        
        for target,games in config.items('Xonstat Games'):
            games = [ x.strip() for x in games.decode('string-escape').split(",") ]
            if gametype in games:
                self.gametype = target.lower()
                break

    def is_valid(self):
        return (self.gametype != None)

    def __str__(self):
        return self.name + ": " + ", ".join([ str(p) for p in self.players])

    def get_players(self):
        return self.players

    def add_player(self, player):
        self.players.append(player)
        elo = player.get_elo(self.gametype)
        if elo:
            self.elo += elo

    def remove_player(self, player):
        for p in self.players:
            if p == player:
                elo = player.get_elo()
                if elo:
                    self.elo -= elo
                self.players.remove(p)
                return True
        return False

    def get_mean_elo(self):
        try:
            return float(self.elo) / len(self.players)
        except:
            pass
        return 0

    def auto_add_player(self, other, pickpool):
        elo_diff = self.get_mean_elo() - other.get_mean_elo()
        elo = None
        player = None
        for p in pickpool:
            p_elo = p.get_elo(self.gametype)
            if not elo or (elo_diff >  0 and p_elo < elo) or (elo_diff <= 0 and p_elo > elo):
                elo = p_elo
                player = p
        print self.name + ": auto-adding {0} ({1} elo)".format(p.nick, p_elo)
        self.add_player(player)
        return player


class Game:
    """A game that can be played in the channel"""
    def __init__(self, pickup, nick, name, captains=2, players=8, autopick=False, externpick=False, **kwargs):
        self.pickup = pickup
        self.nick = nick
        self.name = name
        self.caps = int(captains)
        self.maxplayers = int(players)
        self.autopick = bool(autopick)
        self.extern_pick = bool(externpick)
        self.info = kwargs
        self.players = []
        self.starting = False
        self.abort_start = False
        
        if 'teamnames' in kwargs:
            self.teamnames = config.getlist('Pickup: '+nick, 'teamnames')
        else:
            self.teamnames = []

    def pre_start(self):
        """Initiates a game's start"""
        self.starting = True
        start = self.pickup.pypickupbot.fire('pickup_game_pre_start', self)

        def _knowStart(start):
            if not start:
                self.pickup.pypickupbot.cmsg(
                    _("%s game about to start..")%self.name
                )
            else:
                self.do_start()
        start.addCallback(_knowStart)

    def abort_start(self):
        """Aborts a starting game"""
        if self.starting:
            self.abort_start = True
        return self

    def abort(self):
        """Aborts a starting game"""
        if self.starting:
            self.abort_start = True
        if len(self.players):
            self.players = []
            self.pickup.update_topic()
        self.starting = False
        return self

    def do_start(self):
        """Does the actual starting of the game"""
        if self.abort_start or not self.starting:
            self.abort_start = False
            self.starting = False
            return

        players = self.players[:self.maxplayers]
        for player in players:
            self.pickup.all_games().force_remove(player)

        self.pickup.update_topic()

        self.pickup.pypickupbot.notice(self.pickup.pypickupbot.channel, 
            _("%(gamenick)s game ready to start in %(channel)s")
                % {'gamenick': self.nick, 'channel': self.pickup.pypickupbot.channel})

        captains = []

    def pickup_game_started(self, game, players, captains):
        for p in players:  # flatten list
            if type(p) == list:
                players.remove(p)
                players.extend(p)
        
        gametype = game.nick
        team1, team2 = Team(_("Team 1"), gametype), Team(_("Team 2"), gametype)
        if not (team1.is_valid() and team2.is_valid()):
            return
        
        game.pick_extern = True
        
        pickpool = []
        for p in players:
            nick = self._get_original_nick(p)
            if self.players.has_key(nick):
                player = self.players[nick]
            else:
                player = Player(nick, None)
            pickpool.append(player)
        
        # randomly select one captain, then select other one with similar elo
        team1.captain = random.choice(pickpool)
        best_diff = None
        print pickpool
        for p in pickpool:
            if p == team1.captain:
                continue
            elo_diff = team1.captain.get_elo(gametype) - p.get_elo(gametype)
            if not best_diff or elo_diff < best_diff:
                team2.captain = p
                best_diff = elo_diff
        captains = [ team1.captain, team2.captain ]
        print "Captains:", team1.captain, ",", team2.captain, "(elo diff:", best_diff, ")"
        
        # auto-select players (based on elo)
        while len(pickpool):
            p = team1.auto_add_player(team2, pickpool)
            pickpool.remove(p)
            team1, team2 = team2, team1  # swap

        teams = [ team1, team2 ]
        playerlist  = [str(p) for p in players]
        captainlist = [str(c) for c in captains]

        self.pickup.pypickupbot.cmsg(
            config.get('Pickup messages', 'game ready autopick').decode('string-escape')%
            {
                'nick': game.nick,
                'playernum': len(players),
                'playermax': game.maxplayers,
                'name': game.name,
                'numcaps': game.caps,
                'teamslist': ', '.join([
                    config.get('Pickup messages', 'game ready autopick team').decode('string-escape')%
                    {
                        'name': team.name,
                        'players': ', '.join([ str(p) for p in team.players])
                    }
                    for team in teams])
            })
        if config.getboolean("Pickup", "PM each player on start"):
            for player in players:
                self.pickup.pypickupbot.msg(player, 
                    config.get("Pickup messages", "youre needed").decode('string-escape')%
                    {
                        'channel': self.pickup.pypickupbot.channel,
                        'name': game.name,
                        'nick': game.nick,
                        'numcaps': game.caps,
                        'playerlist': ', '.join(playerlist),
                        'captainlist': ', '.join(captainlist)
                    })

        self.pickup.pypickupbot.fire('pickup_game_started', self, players, captains)
        self.starting = False
        return  ################################################################

        if not self.autopick:
            pickpool = sorted(players)
            captains = random.sample(pickpool,2)

            self.pickup.pypickupbot.fire('pickup_game_starting', self, players, captains)
            if self.extern_pick:
                return
            
            if len( captains ) > 0:
                self.pickup.pypickupbot.msg( self.pickup.pypickupbot.channel,
                    config.get('Pickup messages', 'game ready').decode('string-escape')%
                    {
                        'nick': self.nick,
                        'playernum': len(self.players),
                        'playermax': self.maxplayers,
                        'name': self.name,
                        'numcaps': self.caps,
                        'playerlist': ', '.join(players),
                        'captainlist': ', '.join(captains)
                    })
                if config.getboolean("Pickup", "PM each player on start"):
                    for player in players:
                        self.pickup.pypickupbot.msg(player, 
                            config.get("Pickup messages", "youre needed").decode('string-escape')%
                            {
                                'channel': self.pickup.pypickupbot.channel,
                                'name': self.name,
                                'nick': self.nick,
                                'numcaps': self.caps,
                                'playerlist': ', '.join(players),
                                'captainlist': ', '.join(captains)
                            })
            else:
                self.pickup.pypickupbot.msg( self.pickup.pypickupbot.channel,
                    config.get('Pickup messages', 'game ready nocaptains').decode('string-escape')%
                    {
                        'nick': self.nick,
                        'playernum': len(self.players),
                        'playermax': self.maxplayers,
                        'name': self.name,
                        'numcaps': self.caps,
                        'playerlist': ', '.join(players)
                    })
                if config.getboolean("Pickup", "PM each player on start"):
                    for player in players:
                        self.pickup.pypickupbot.msg(player, 
                            config.get("Pickup messages", "youre needed nocaptains").decode('string-escape')%
                            {
                                'channel': self.pickup.pypickupbot.channel,
                                'name': self.name,
                                'nick': self.nick,
                                'numcaps': self.caps,
                                'playerlist': ', '.join(players),
                            })
        else:
            teams = [[] for i in range(self.caps)]
            players_ = sorted(players)
            for i in range(len(players)):
                player = random.choice(players_)
                players_.remove(player)
                teams[i % self.caps].append(player)

            self.pickup.pypickupbot.fire('pickup_game_starting', self, teams, captains)
            if self.extern_pick:
                return
            
            self.pickup.pypickupbot.cmsg(
                config.get('Pickup messages', 'game ready autopick').decode('string-escape')%
                {
                    'nick': self.nick,
                    'playernum': len(players),
                    'playermax': self.maxplayers,
                    'name': self.name,
                    'numcaps': self.caps,
                    'teamslist': ', '.join([
                        config.get('Pickup messages', 'game ready autopick team').decode('string-escape')%
                        {
                            'name': self.teamname(i),
                            'players': ', '.join(team)
                        }
                        for i, team in enumerate(teams)])
                })

        self.pickup.pypickupbot.fire('pickup_game_started', self, players, captains)

        self.starting = False

    def teamname(self, i):
        if len(self.teamnames) > i:
            return self.teamnames[i]
        else:
            return _("Team {0}").format(i+1)

    def add(self, call, user):
        """Add a player to this game"""
        if user not in self.players:
            self.players.append(user)
            self.pickup.update_topic()
        
        if len(self.players) >= self.maxplayers:
            self.pre_start()
            return False

    def who(self):
        """Who is in this game"""
        if len(self.players):
            return config.get('Pickup messages', 'who game').decode('string-escape') % {'nick': self.nick, 'playernum': len(self.players), 'playermax': self.maxplayers, 'name': self.name, 'numcaps': self.caps, 'playerlist': ', '.join(self.players) }

    def remove(self, call, user):
        """Removes a player from this game"""
        if user in self.players:
            if not self.starting or len(self.players) > self.maxplayers:
                self.players.remove(user)
                self.pickup.update_topic()
            else:
                call.reply(_('Too late to remove from %s') % self.nick)

    def force_remove(self, user):
        try:
            self.players.remove(user)
            self.pickup.update_topic()
        except ValueError:
            pass

    def rename(self, oldnick, newnick):
        """Rename a player"""
        try:
            i = self.players.index(oldnick)
            self.players[i] = newnick
        except ValueError:
            pass

class Games(Game):
    """Groups multiple games to dispatch calls"""
    def __init__(self, games):
        self.games = games

    def remove(self, *args):
        for game in self.games: game.remove(*args)
    def pre_start(self, *args):
        for game in self.games: game.pre_start(*args)
    def force_remove(self, *args):
        for game in self.games: game.force_remove(*args)
    def rename(self, *args):
        for game in self.games: game.rename(*args)

    def add(self, *args):
        for game in self.games:
            if game.add(*args) == False:
                break

    def who(self, *args):
        return [game.who(*args) for game in self.games]

    def abort(self, *args):
        for game in self.games:
            game.abort()
        return self

    def abort_start(self, *args):
        for game in self.games:
            game.abort_start()
        return self


class XonstatInterface:

    def __init__(self):
        self.players = {}       # nick : playerid
        self.nickchanges = {}   # newname : originalname

        def _done(args):
            self._load_from_db()
        d = db.runOperation("""
            CREATE TABLE IF NOT EXISTS
            xonstat_players
            (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                nick        TEXT,
                playerid    INTEGER,
                create_dt   INTEGER
            )""")
        d.addCallback(_done)
        
    def _load_from_db(self):
        d = db.runQuery("""
            SELECT      nick, playerid, create_dt
            FROM        xonstat_players
            ORDER BY    create_dt
            """)
        self.players = {}
        def _loadPlayers(r):
            for entry in r:
                nick, playerid, create_dt = entry
                self.players[nick] = Player(nick, playerid, create_dt)
        return d.addCallback(_loadPlayers)

    def _find_player(self, nick):
        if self.players.has_key(nick):
            return self.players[nick]
        return None

    def _purge(self, keep=0):
        """used by clearPlayers and purgePlayers"""
        d = defer.gatherResults(
            db.runOperation("""
                DELETE FROM xonstat_players
                WHERE       create_dt < ?
                """, (itime() - keep,))
            )
        def onErr(failure):
            log.err(failure, "purge players, keep = {0}".format(keep))
            return failure
        d.addErrback(onErr)
        return d

    def _delete(self, nick):
        return db.runOperation("""
            DELETE FROM xonstat_players
            WHERE       nick=?
            """, (nick,))

    def _insert(self, nick, playerid):
        return db.runOperation("""
            INSERT INTO xonstat_players(nick, playerid, create_dt)
            VALUES      (:nick, :playerid, :ctime)
            """, (nick, playerid, itime(),))

    def _get_nick(self, nick):
        if self.nickchanges.has_key(nick):
            return self.nickchanges[nick]
        return nick

    def _get_original_nick(self, nick):
        for old,new in self.nickchanges.items():
            if new == nick:
                return old
        return nick

    def _rename_user(self, oldname, newname):
        original = self._get_original_nick(oldname)
        if newname == original:
            if self.xonstat.nickchanges.has_key(oldname):
                del self.nickchanges.has_key[oldname]
        else:
            self.nickchanges[original] = newname


class XonstatPickupBot:
    """Allows the bot to run games with captain-picked teams"""

    def all_games(self):
        """Gets a wrapper for all games"""
        return Games(self.games.values())

    def get_games(self, call, args, implicit_all=True):
        """Gets all or some games"""
        if len(args):
            games = []
            for arg in args:
                arg = arg.lower()
                if arg in self.games:
                    games.append(self.games[arg])
                else:
                    raise InputError(_("Game %s doesn't exist. Available games are: %s") % (arg, ', '.join(self.games.keys())))
            return Games(games)
        elif implicit_all:
            return Games(self.games.values())
        else:
            raise InputError(_("You need to specify a game."))

    def get_game(self, call, args):
        """Get one game"""
        if len(args) == 1:
            game = args[0].lower()
            if game in self.games:
                return self.games[game]
            else:
                raise InputError(_("Game %s doesn't exist. Available games are: %s") % (args[0], ', '.join(self.games.keys())))
        else:
            if len(args) > 1:
                raise InputError(_("This command only allows one game to be selected."))
            else:
                raise InputError(_("This command needs one game to be selected."))

    def update_topic(self):
        """Update the pickup part of the channel topic"""
        config_topic = config.getint('Pickup', 'topic')

        if not config_topic:
            return

        out = []
        for gamenick in self.order:
            game = self.games[gamenick]
            if config_topic == 1 or game.players:
                out.append(
                    config.get('Pickup messages', 'topic game').decode('string-escape')
                    % {
                        'nick': game.nick, 'playernum': len(game.players),
                        'playermax': game.maxplayers, 'name': game.name,
                        'numcaps': game.caps
                    })

        self.topic.update(
            config.get('Pickup messages', 'topic game separator')\
                .decode('string-escape')\
            .join(out)
            )

    def add(self, call, args):
        """!add [game [game ..]]

        Signs you up for one or more games"""
        self.get_games(call, args,
            config.getboolean("Pickup", "implicit all games in add"))\
            .add(call, call.nick)

    def remove(self, call, args):
        """!remove [game [game ..]]

        Removes you from one or more games"""
        self.get_games(call, args).remove(call, call.nick)

    def who(self, call, args):
        """!who [game [game ..]]
        
        Shows who has signed up"""
        games = [i for i in self.get_games(call, args).who() if i != None]
        all = False
        if len(args) < 1 or len(args) == len(self.games):
            all = True
        if len(games):
            if all:
                call.reply(_("All games:")+" "+config.get('Pickup messages', 'who game separator').decode('string-escape').join(games),


                    config.get('Pickup messages', 'who game separator').decode('string-escape'))
            else:
                call.reply(config.get('Pickup messages', 'who game separator').decode('string-escape').join(games),
                    config.get('Pickup messages', 'who game separator').decode('string-escape'))
        else:
            if all:
                call.reply(_("No game going on!"))
            else:
                call.reply(_n("No game in mode %s", "No game in modes %s", len(args)) % ' '.join(args) )
        
    def promote(self, call, args):
        """!promote <game>

        Shows a notice encouraging players to sign up for the specified game"""
        admin = self.pypickupbot.is_admin(call.user, call.nick)

        def _knowAdmin(admin):
            if self.last_promote + config.getint('Pickup', 'promote delay') > time() \
                    and not admin:
                raise InputError(_("Can't promote so often."))

            game = self.get_game(call, args)

            if call.nick not in game.players \
                    and not admin:
                raise InputError(_("Join the game yourself before promoting it."))

            self.last_promote = time()
            self.pypickupbot.cmsg(
                config.get('Pickup messages', 'promote').decode('string-escape') % {
                    'bold': '\x02', 'prefix': config.get('Bot', 'command prefix'),
                    'name': game.name, 'nick': game.nick,
                    'command': config.get('Bot', 'command prefix')+'add '+game.nick,
                    'channel': self.pypickupbot.channel,
                    'playersneeded': game.maxplayers-len(game.players),
                    'maxplayers': game.maxplayers, 'numplayers': len(game.players),
                })
        return admin.addCallbacks(_knowAdmin)

    def pull(self, call, args):
        """!pull <player> [game [game ..]]

        Removes someone else from one or more games"""
        player = args.pop(0)
        games = self.get_games(call, args).force_remove(player)

    def force_start(self, call, args):
        """!start <game>

        Forces the game to start, even if not enough players signed up"""
        game = self.get_game(call, args)
        if len(game.players) >= game.caps:
            game.pre_start()
        else:
            call.reply(_("Not enough players to choose captains from."))

    def abort(self, call, args):
        """!abort [game [game ..]]
        
        Aborts a game"""
        self.get_games(call, args).abort()

    def pickups(self, call, args):
        """!pickups [search]

        Lists games available for pickups.
        """
        call.reply( ', '.join(
                ["%s (%s)" % (nick, game.name)
                for nick, game in self.games.iteritems()
                if not args or args[0] in nick or args[0] in game.name.lower()]
            ), ', ')

    def clearPlayers(self, call, args):
        """!clearplayers
        
        Clears the registered players list completely.
        Prefer the purgeplayers command to this."""
        d = call.confirm("This will delete all registered players, continue?")
        def _confirmed(ret):
            if ret:
                def done(*args):
                    call.reply(_("Done."))
                return self.xonstat._purge().addCallback(done)
            else:
                call.reply(_("Cancelled."))
        return d.addCallback(_confirmed)

    def purgePlayers(self, call, args):
        """!purgeplayers <keep>

        Purges the registered players list from entries created later than
        [keep] ago."""
        if not len(args) == 1:
            raise InputError(_("You need to specify a time range."))
        try:
            keep = timediff_from_str(' '.join(args))
        except InvalidTimeDiffString as e:
            raise InputError(e)
        d = call.confirm(
            "This will delete the record of all players registered later than {0}, continue?".format(str_from_timediff(keep))
            )
        def _confirmed(ret):
            if ret:
                def done(*args):
                    call.reply(_("Done."))
                self.xonstat._purge(keep).addCallback(done)
            else:
                call.reply(_("Cancelled."))
        d.addCallback(_confirmed)

    def listPlayers(self, call, args):
        players = self.xonstat.players
        if len(players) == 0:
            call.reply(_("No players registered yet."))
            return
        
        sep = config.get("Xonstat Interface", "playerlist separator").decode('string-escape')
        reply = config.get("Xonstat Interface", "playerlist").decode('string-escape')%\
                { 'players': sep.join(players.keys()), 'num_players': len(players), }
        call.reply(reply)

    def playerInfo(self, call, args):
        if not len(args) == 1:
            raise InputError("You need to specify a player nickname.")
        
        nick = self.xonstat._get_original_nick(args[0])
        player = self.xonstat._find_player(nick)
        if not player:
            call.reply(_("No player named <{0}> found!").format(nick))
            return
        
        sep = config.get("Xonstat Interface", "playerinfo separator").decode('string-escape')
            
        elo_list = []
        for gametype,elo in player.get_elo_dict().items():
            eloscore, games = round(elo['elo'], 1), elo['games']
            if games >= 32:
                elo_list.append( _("{0}: {1}").format(gametype.upper(), eloscore) )
            else:
                elo_list.append( _("{0}: {1}*").format(gametype.upper(), eloscore) )
        elo_list.sort()
        elo_display = sep.join(elo_list)
        if len(elo_list) == 0:
            elo_display = _("none yet")
        
        rank_list = []
        for gametype,rank in player.get_rank_dict().items():
            rank, max_rank = rank['rank'], rank['max_rank']
            if max_rank:
                rank_list.append( _("{0}: {1} of {2}").format(gametype.upper(), rank, max_rank) )
            else:
                rank_list.append( _("{0}: {1}").format(gametype.upper(), rank) )
        rank_list.sort()
        rank_display = sep.join(rank_list)
        if len(rank_list) == 0:
            rank_display = _("none yet")
        
        reply = config.get("Xonstat Interface", "playerinfo").decode('string-escape')%\
                { 'nick': nick, 'gamenick': player.get_nick(), }
        if len(elo_list) > 0:
            reply += config.get("Xonstat Interface", "playerinfo elo").decode('string-escape')%\
                { 'elos': elo_display }
        if len(rank_list) > 0:
            reply += config.get("Xonstat Interface", "playerinfo rank").decode('string-escape')%\
                { 'ranks': rank_display, }
        reply += config.get("Xonstat Interface", "playerinfo profile").decode('string-escape')%\
            { 'profile': player.get_xonstat_url(), }
        call.reply(reply)

    def playerExists(self, call, args):
        nick = self.xonstat._get_original_nick(args[0])
        player = self.xonstat._find_player(nick)
        if player:
            reply = _("This nick is registered with player id #{0} (as \x02{1}\x02). " + \
                    "Use \x02!playerinfo <nick>\x02 to see more details.").\
                    format(player.get_id(), nick)
        else:
            reply = _("No player information found for <{0}>.".format(nick))
        call.reply(reply)

    def register(self, call, args):
        if not len(args) == 1:
            raise InputError(_("You must specify your Xonstat profile id to register an account."))
        
        nick = self.xonstat._get_original_nick(call.nick)
        player = self.xonstat._find_player(nick)
        if player:
            raise InputError(_("This nick is already registered with player id #{0} (as <{1}>) - can't continue! " + \
                    "If you need to change your player id, please contact one of the channel operators.").\
                    format(player.get_id(), nick))
        try:
            playerid = int(args[0])
        except ValueError:
            raise InputError(_("Player id must be an integer."))
        
        player = Player(nick, playerid)
        if not player.get_nick():
            raise InputError(_("This doesn't seem to be a valid Xonstat playerid!"))
        
        d = call.confirm(_("You're about to register yourself with player id #{0} (\x02{1}\x02, " + \
                "Xonstat profile {2}), is this correct?").\
                format(player.get_id(), player.get_nick(), player.get_xonstat_url()))
        def _confirmed(ret):
            if ret:
                def done(*args):
                    def done(*args):
                        player = self.xonstat._find_player(nick)
                        call.reply("Done.")
                        msg = config.get('Xonstat Interface', 'player registered').decode('string-escape')%\
                            { 'nick': nick, 'playerid': playerid, 'gamenick': player.get_nick(), 'profile': player.get_xonstat_url(), }
                        self.pypickupbot.msg( self.pypickupbot.channel, msg.encode('ascii') )
                    self.xonstat._load_from_db().addCallback(done)
                self.xonstat._insert(nick, playerid).addCallback(done)
            else:
                call.reply(_("Cancelled."))
        d.addCallback(_confirmed)

    def removePlayer(self, call, args):
        if not len(args) == 1:
            raise InputError(_("You need to specify one player name."))
        nick = args[0]
        d = call.confirm(_("This will delete all entries registered to {0}, continue?").format(nick))
        def _confirmed(ret):
            if ret:
                def done(*args):
                   def done(*args):
                        call.reply(_("Done."))
                   d = self._load_from_db()
                   d.addCallback(done)
                return self.xonstat._delete(nick).addCallback(done)
            else:
                call.reply(_("Cancelled."))
        return d.addCallback(_confirmed)

    def __init__(self, bot):
        """Plugin init

        Reads games from config"""
        self.xonstat = XonstatInterface()
        
        self.games = {}
        self.order = []
        self.last_promote = 0
        if not config.has_section('Pickup games'):
            log.err('Could not find section "Pickup games" of the config!')
            return
        games = config.items('Pickup games')
#        log.msg(games)

        for (gamenick, gamename) in games:
            if gamenick == 'order':
                self.order = config.getlist('Pickup games', 'order')
            else:
                if config.has_section('Pickup: '+gamenick):
                    gamesettings = dict(config.items('Pickup: '+gamenick))
                else:
                    gamesettings = {}
                self.games[gamenick] = Game(
                    self,
                    gamenick, gamename,
                    **gamesettings)
                if gamenick not in self.order:
                    self.order.append(gamenick)

        self.order = filter(lambda x: x in self.games, self.order)

    def joinedHomeChannel(self):
        """when home channel joined, set topic"""
        if config.get('Pickup', 'topic'):
            self.topic = self.pypickupbot.topic.add('', Topic.GRAVITY_BEGINNING)
            self.update_topic()

    def userRenamed(self, oldname, newname):
        """track user renames"""
        self.xonstat._rename_user(oldname, newname)
        self.all_games().rename(oldname, newname)

    def userLeft(self, user, channel, *args):
        """track quitters"""
        if channel == self.pypickupbot.channel:
            self.all_games().force_remove(user.split('!')[0])
    
    def userQuit(self, user, quitMessage):
        """track quitters"""
        self.all_games().force_remove(user.split('!')[0])

    commands = {
        'add':              (add,           COMMAND.NOT_FROM_PM),
            'addup':        (add,           COMMAND.NOT_FROM_PM),
        'remove':           (remove,        COMMAND.NOT_FROM_PM),
            'leave':        (remove,        COMMAND.NOT_FROM_PM),
            'logout':       (remove,        COMMAND.NOT_FROM_PM),
        'who':              (who,           0),
        'promote':          (promote,       COMMAND.NOT_FROM_PM),
        'pull':             (pull,          COMMAND.NOT_FROM_PM | COMMAND.ADMIN),
        'start':            (force_start,   COMMAND.NOT_FROM_PM | COMMAND.ADMIN),
        'abort':            (abort,         COMMAND.NOT_FROM_PM | COMMAND.ADMIN),
        'pickups':          (pickups,       0),
        
        'register':         (register,      COMMAND.NOT_FROM_PM),
        'playerinfo':       (playerInfo,    0),
        'player':           (playerExists,  0),
        'listplayers':      (listPlayers,   0),
        'removeplayer':     (removePlayer,  COMMAND.NOT_FROM_PM | COMMAND.ADMIN),
        'clearplayers':     (clearPlayers,  COMMAND.NOT_FROM_PM | COMMAND.ADMIN),
        'purgeplayers':     (purgePlayers,  COMMAND.NOT_FROM_PM | COMMAND.ADMIN),
        }

    eventhandlers = {
        'joinedHomeChannel':    joinedHomeChannel,
        'userRenamed':          userRenamed,
        'userLeft':             userLeft,
        'userKicked':           userLeft,
        'userQuit':             userQuit,
    }

xonstat_pickup = SimpleModuleFactory(XonstatPickupBot)
