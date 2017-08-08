#!/usr/bin/env python

__version__ = "2.0-beta15"

__doc__ = """\
OneTime: an open source encryption program that uses the one-time pad method.

(Run 'onetime --help' for usage information.)

The usual public-key encryption programs, such as GnuPG, are probably
secure for everyday purposes, but their implementations are too
complex for all but the most knowledgeable programmers to vet, and
in some cases there may be vulnerable steps in the supply chain
between their authors and the end user.  When bootstrapping trust,
it helps to start with something you can trust by inspection.

Hence this script, OneTime, a simple program that encrypts plaintexts
against one-time pads.  If you don't know what one-time pads are, this
program may not be right for you.  If you do know what they are and
how to use them, this program can make using them more convenient.

OneTime handles some of the pad-management bureaucracy for you.  It
avoids re-using pad data -- except when decrypting the same message
twice -- by maintaining records of pad usage in ~/.onetime/pad-records.
(The pads themselves are not typically stored there, just records
about pad usage.)

Recommended practice: if you are Alice communicating with Bob, then
keep two different pads, 'alice_to_bob.pad' and 'bob_to_alice.pad', as
opposed to sharing the same pad for both directions of communication.
With two separate pads, even if you each send a message simultaneously
to the other with no advance planning, you still won't accidentally
use any of the same pad data twice, assuming you let OneTime do its
bookkeeping naturally.

See http://en.wikipedia.org/wiki/One-time_pad for more information
about one-time pads in general.

OneTime is written by Karl Fogel and distributed under an MIT-style
open source license; run 'onetime --license' or see the LICENSE file
in the full distribution for complete licensing information.
OneTime's home page is http://www.red-bean.com/onetime/.
"""

import os
import sys
import stat
import getopt
import bz2
import base64
import hashlib
import re
import xml
import xml.dom
import xml.dom.minidom
import random


########################### Design Overview #############################
#
# To encrypt, OneTime first compresses input using bzip2, then XOR's
# the bzipped stream against the pad, and emits the result in base64
# encoding.  It also records in ~/.onetime/pad-records that that
# length of pad data, starting from a particular offset, has been
# used, so that the user won't ever re-use that stretch of pad for
# encryption again.
#
# To decrypt, OneTime does the reverse: base64-decode the input, XOR
# it against the pad (starting at the pad offset specified in the
# encrypted message), and bunzip2 the result.  Decryption also records
# that this range has been used, in ~/.onetime/pad-records, because
# the recipient of a message should of course never use the same
# pad data to encrypt anything else.
#
# The output format looks like this:
#
# -----BEGIN OneTime MESSAGE-----
# Format: internal  << NOTE: OneTime 1.x and older cannot read this format. >>
# Pad ID: [...64 hexadecimal digits of unique pad ID...]
# Offset: [...a number expressed in decimal...]
#
# [...encrypted block, base64-encoded...]
#
# -----END OneTime MESSAGE-----
#
# The encrypted block has some structure, though -- it's not *just* a
# base64 encoding of pad-XOR'd bzipped plaintext data.  Before and
# after the core data, there's some bookkeeping, all base64-encoded.
# Here's a diagram, with index increasing from left to right:
#
#   FFHHTTRRRR******-------------------------------------DDDD*******
#
# The precise number of same characters in a row is not significant
# above, except as a rough guide to the relative lengths of the
# different sections.  This is what the different sections mean:
#
#   F  ==  a few format indicator bytes:
#
#          These tell OneTime what version of its internal format it
#          is looking at.  This is really just for future-proofing,
#          since these internal format indicator bytes were only
#          introduced in 2.0, and as of this writing OneTime is still
#          using that first format (known as "internal format 0").
#
#   H  ==  a few head fuzz source length bytes:
#
#          A few bytes of raw pad data, used to calculate
#          the number of bytes of head fuzz (the first
#          set of asterisks) that will be used.
#
#   T  ==  a few tail fuzz source length bytes:
#
#          A few more bytes of raw pad data, used to calculate
#          the number of bytes of tail fuzz (the concluding
#          set of asterisks) that will be used.
#
#   R  ==  some bytes of raw pad input for the session hash
#
#          These bytes are among the data fed to the session hash, so
#          that the final message digest authenticates the pad as well
#          as the plaintext.  (The actual number of bytes used is
#          PadSession._digest_source_length.)
#
#   *  ==  a random number of fuzz bytes (head fuzz or tail fuzz):
#
#          A random number (derived from the pad -- see above) of
#          runtime-generated random or pseudo-random bytes, XOR'd
#          against the same number of pad bytes.
#
#          When these encrypted bytes appear at the front, they are
#          called "head fuzz", and because the number of bytes is
#          based on pad data, they prevent an attacker from knowing
#          exactly where in the encrypted text the message starts.
#          When they appear at the end they are called "tail fuzz",
#          and similarly, they mean that an attacker does not know
#          exactly where the message ends.
#
#          In other words, the real message (and its digest, described
#          below) sits somewhere along a slider surrounded by fuzz on
#          each side, and the precise amount of fuzz on each side is
#          known only to those with the pad.  This prevents a
#          known-plaintext message substitution attack: because
#          attackers cannot know where the message is, they cannot
#          reliably replace a known plaintext with some other
#          plaintext of the same length, even with channel control.
#
#          The reason the fuzz regions are random data XOR'd against
#          pad, instead of just being plain pad data (which would have
#          been theoretically sufficient) is to avoid exposing weak
#          pads.  Even though it would be a bad mistake for a user to
#          use a merely pseudo-random pad, instead of a truly random
#          one, still at least OneTime should do its best to avoid
#          exposing this mistake in an obvious way when it happens.
#
#   -  ==  encrypted plaintext
#
#          Base64-encoded XOR'd bzipped plaintext.
#
#   D  ==  32 bytes of digest
#
#          A SHA256 digest, XOR'd with pad, of: the raw pad session
#          hash input bytes, plus the raw head fuzz (that is, the
#          random bytes from *before* they were XOR'd with the pad to
#          produce the final head fuzz), plus the plaintext message.
#
#          The purpose of this digest verify message integrity without
#          revealing anything about the pad (because we don't want an
#          attacker to be able to analyze whether the pad itself might
#          have any weaknesses).  Using a combined digest means that
#          even in the case of a known plaintext there is no plaintext
#          substitution attack and no way to recover any of the raw
#          pad bytes.
#
#   (Note that this is not a hexadecimal representation of the hash;
#   to save space, it is the raw hash digest.  However, any integrity
#   errors display the hash in hexadecimal.)
#
# On decryption, OneTime verifies both the digest and that the tail
# fuzz is exactly as long as expected.  If anything doesn't match,
# OneTime will raise an error -- though if the error was detected only
# in the digest or in the tail fuzz length, then there may still have
# been a successful decryption first, with plaintext output emitted.
#
# OneTime takes care never to expose even raw pad bytes that aren't
# going to be used for encryption.  Although in theory the pad should
# be completely random, and therefore an exposure of pad bytes that
# aren't used for encryption should not reveal anything about other
# bytes that *are* used for encryption, in practice, if someone is
# using a pad that is not perfectly random, we don't want OneTime to
# make that situation any worse than it has to be.  Therefore, in two
# places that could potentially expose such raw pad bytes, we don't:
#
#   1) To calculate the pad ID (which is recorded in the file
#      ~/.onetime/pad-records, which we have to assume could get
#      exposed to an attacker), we use the hexadecimal digest of a
#      SHA256 hash of some bytes from the front of the pad, instead of
#      just using a hex representation of those bytes in raw form.
#
#   2) When inserting head fuzz and tail fuzz into the encrypted
#      message, we don't just use raw pad bytes, but rather XOR raw
#      pad bytes against run-time-generated random bytes, so that the
#      fuzz regions reveal as little as possible about the pad and
#      are, ideally, in no way distinguishable on inspection from the
#      encrypted data between them.
#
# As for the code, the design is pretty much what you'd expect:
#
# The PadSession class encapsulates one session of using a pad to
# encrypt or decrypt one contiguous data stream.  It takes care of
# everything that happens in the encrypted block above.
#
# A PadSession object is created with a particular pad file and
# registered with a Configuration object.  The Configuration object
# reads the ~/.onetime/pad-records file, to ensure that the PadSession
# starts from the right offset in the pad, and it records in
# ~/.onetime/pad-records that a new stretch of pad is consumed, once
# the PadSession is done.
#
# A PadSession is wrapped with a SessionEncoder if encrypting or with
# a Sessiondecoder if decrypting.  These wrapper classes take care of
# the base64 encoding.
#
########################################################################

# The current format level.
#
# Some background is needed to understand what this means:
#
# The first releases of OneTime (the 1.x series) did not include any
# indication of the format in the plaintext headers of the output.
# This was deliberate: after all, if there *were* a format change in
# the future, a "Format:" header could be added, and its presence
# would indicate that the output was clearly from a later version than
# the 1.x series.
#
# Well, that has now happened -- but instead of specifying an exact
# format version in the plaintext header, we just specify that the
# format is "internal", and in the code we call that a "format level"
# instead of a "format version".  We distinguish between this new
# "internal" level and the old level (now retroactively labeled
# "original"), for the purpose of supporting OneTime 1.x and earlier,
# but beyond that the plaintext header does not say anything about the
# format other than that label "internal".
#
# There are a couple of reasons to do it this way.  One, to get away
# from the idea that the version of OneTime is relevant, since that
# what really matters is just the output format -- which can and often
# will remain unchanged, or at least backward-compatible, from version
# to version of OneTime.  Two, starting from OneTime 2.0, all detailed
# format version information is embedded in "inner headers" in the
# ciphertext (see the PadSession class for details), not in the plaintext
# headers.  This avoids leaking information about the earliest
# possible date on which the message could have been encrypted,
# because the ciphertext will reveal only that the message must have
# been encrypted with OneTime 2.0 or higher.
#
# Therefore, in OneTime's code, instead of using numbers for the
# format level, we use one of two words: "internal" or "original".
#
# Note also that the "original" format had a (rather embarrassing) bug
# whereby plaintext was encrypted and then compressed, instead of the
# other way around.  This is fixed in all the "internal" level
# formats, and of course any further format details are now embedded
# in the inner headers in the cipthertext, as described in class PadSession.
# (And no, http://blog.appcanary.com/2016/encrypt-or-compress.html
# does not contradict that compress-then-encrypt is right for OneTime.)
Format_Level = "internal"


class Configuration:
  """A parsed representation of one user's ~/.onetime/ configuration area.
  A .onetime/ directory contains just a 'pad-records' file right now.

  Even in cases where we're operating without touching permanent
  storage, a Configuration instance is still created and updated
  internally.  This is partly because the Configuration does some
  consistency checks on incoming/outgoing data, and partly because it
  would be useful if we're ever providing an API.
  """

  class ConfigurationError(Exception):
    """Exception raised if we encounter an impossible state in a
    Configuration."""
    pass

  def __init__(self, pad_session, path=None):
    """Initialize a new configuration with PAD_SESSION.

    If PATH is None, try to find or create the config area in the
    standard location in the user's home directory; otherwise, find or
    create it at PATH.

    If PATH is \"-\", instantiate a Configuration object but do not
    connect it to any activity on disk; it will neither read from nor
    write to permanent storage."""

    self._pad_session = pad_session
    self.config_area = path
    if self.config_area is None:
      self.config_area = os.path.join(os.path.expanduser("~"), ".onetime")
    self.pad_records_file = os.path.join(self.config_area, "pad-records")
    # Create the configuration area if necessary.
    if self.config_area != '-' and not os.path.isdir(self.config_area):
      # Legacy data check: if they have a ~/.otp dir, that's probably
      # from a previous incarnation of this program, when it was named
      # "otp".  If so, just rename the old config area.
      old_config_area = os.path.join(os.path.expanduser("~"), ".otp")
      old_pad_records_file = os.path.join(old_config_area, "pad-records")
      if os.path.isfile(old_pad_records_file):
        os.rename(old_config_area, self.config_area)
      else:
        os.mkdir(self.config_area)

    # Create the pad-records file if necessary.
    if self.config_area != '-' and not os.path.isfile(self.pad_records_file):
      open(self.pad_records_file, "w").close()
    # Parse the pad-records file (if any) in the configuration area.
    self.pad_records = self._parse_pad_records_file()

  def _consolidate_used_ranges(self, used, allow_reconsumption=False):
    """Return a consolidated version of USED.  USED is a list of
    tuples, indicating offsets and lengths:

       [ (OFFSET1, LENGTH1), (OFFSET2, LENGTH2), ... ]

    Consolidation means returning a list of equal or shorter length,
    that marks exactly the same ranges as used, but expressed in the
    most compact way possible.  For example:

       [ (0, 10), (10, 20), (20, 25) ]

    would become

       [ (0, 25) ]

    If ALLOW_RECONSUMPTION is False, raise a ConfigurationError
    exception if the input is incoherent, such as a range beginning
    inside another range.  But if ALLOW_RECONSUMPTION is True, allow
    ranges to overlap.  Typically, it will be False when encrypting and
    True when decrypting, because it's legitimate to decrypt a message
    multiple times, as long as no one re-uses that range for encrypting."""
    new_used = [ ]
    last_offset = None
    last_length = None

    for tup in used:
      (this_offset, this_length) = tup
      if last_offset is not None:
        if last_offset + last_length >= this_offset:
          # It's only reconsumption if the end of the previous range
          # extends past the next offset.  So we error on that if
          # we're not allowing reconsumption...
          if (last_offset + last_length > this_offset
              and not allow_reconsumption):
            raise self.ConfigurationError(
              "pad's used ranges are incoherent:\n   %s" % str(used))
          # ...but otherwise we just extend the range from the
          # original offset, whether it was a true overlap or a
          # snuggle-right-up-against kind of thing:
          else:
            # All the possible cases are:
            #
            #   1) first tuple entirely precedes second
            #   2) second tuple begins inside first but ends after it
            #   3) second tuple begins and ends inside first
            #   4) second tuple begins *before* first and ends in it
            #   5) second tuple begins and ends before first
            #
            # However, due to the conditional above, we must be in (2)
            # or (3), and we only need to adjust last_length if (2).
            if (this_offset + this_length) > (last_offset + last_length):
              last_length = (this_offset - last_offset) + this_length
        else:
          new_used.append((last_offset, last_length))
          last_offset = this_offset
          last_length = this_length
      else:
        last_offset = this_offset
        last_length = this_length
    if last_offset is not None:
      new_used.append((last_offset, last_length))
    return new_used

  def _get_next_offset(self, used):
    """Return the next free offset from USED, which is assumed to be in
    consolidated form.  PadSession._id_source_length is the minimum
    returned; that way the pad ID stretch is always accounted for,
    even if USED was initialized from an old original-format pad record."""
    cur_offset = None
    # We don't do anything fancy, just get the earliest available
    # offset past the last used tuple.  This means that any ranges in
    # between tuples are wasted.  See comment in main() about
    # discontinuous ranges for why this is okay.
    for tup in used:
      (this_offset, this_length) = tup
      cur_offset = this_offset + this_length
    if cur_offset is None or cur_offset < PadSession._id_source_length:
      return PadSession._id_source_length
    else:
      return cur_offset

  def _parse_pad_records_file(self):
    """Return a dictionary representing this configuration's 'pad-records'
    file (e.g., ~/.onetime/pad-records).  If the file is empty, just
    return an empty dictionary.

    The returned dictionary is keyed on pad IDs, with sub-dictionaries
    as values.  Each sub-dictionary's keys are the remaining element
    names inside a pad element, and the value of the 'used' element is
    a list of tuples, each tuple of the form (OFFSET, LENGTH).  So:

       returned_dict[PAD_ID] ==> subdict
       subdict['used'] ==> [(OFFSET1, LENGTH1), (OFFSET2, LENGTH2), ...]
       subdict['some_elt_name'] ==> SOME_ELT_VALUE       <!-- if any -->
       subdict['another_elt_name'] ==> ANOTHER_ELT_VALUE <!-- if any -->

    A 'pad-records' file is an XML document like this:

      <?xml version="1.0" encode="UTF-8"?>
      <!DOCTYPE TYPE_OF_DOC SYSTEM/PUBLIC "dtd-name">
      <onetime-pad-records>
         <pad-record>
           <id>PAD_ID</id>
           <used><offset>OFFSET_A</offset>
                 <length>LENGTH_A</length></used>
           <used><offset>OFFSET_B</offset>
                 <length>LENGTH_B</length></used>
           ...
         </pad-record>
         <pad-record>
           <id>SOME_OTHER_PAD_ID</id>
           <used><offset>OFFSET_C</offset>
                 <length>LENGTH_C</length></used>
           ...
         </pad-record>
         ...
      </onetime-pad-records>
      """
    dict = { }

    if self.config_area == '-':
      return dict

    try:
      dom = xml.dom.minidom.parse(self.pad_records_file)

      for pad in dom.firstChild.childNodes:
        id = None
        path = None
        used = [ ]
        if pad.nodeType == xml.dom.Node.ELEMENT_NODE:
          subdict = { }
          for pad_part in pad.childNodes:
            if pad_part.nodeType == xml.dom.Node.ELEMENT_NODE:
              if pad_part.nodeName == "id":
                id = pad_part.childNodes[0].nodeValue
              elif pad_part.nodeName == "used":
                offset = None
                length = None
                for used_part in pad_part.childNodes:
                  if used_part.nodeName == "offset":
                    offset = int(used_part.childNodes[0].nodeValue)
                  if used_part.nodeName == "length":
                    length = int(used_part.childNodes[0].nodeValue)
                used.append((offset, length))
                subdict["used"] = self._consolidate_used_ranges(used)
              else:
                # Parse unknown elements transparently.
                subdict[pad_part.nodeName] = pad_part.childNodes[0].nodeValue
          if not subdict.has_key("used"):
            # We don't require the "used" element to be present; if it's
            # absent, it just means none of this pad has been used yet.
            subdict["used"] = [ (0, 0) ]
          dict[id] = subdict
    except xml.parsers.expat.ExpatError:
      pass
    return dict

  def save(self):
    """Save the pad-records file."""
    if self.config_area == '-':
      return
    tempfile = self.pad_records_file + ".tmp"
    # Deliberately not setting binary mode here; this is a text file.
    fp = open(tempfile, 'w')
    fp.write("<onetime-pad-records>\n")
    for pad_id in self.pad_records.keys():
      fp.write("  <pad-record>\n")
      fp.write("    <id>%s</id>\n" % pad_id)
      for tuple in self._consolidate_used_ranges(
          self.pad_records[pad_id]["used"]):
        fp.write("    <used><offset>%d</offset>\n" % tuple[0])
        fp.write("          <length>%d</length></used>\n" % tuple[1])
      for key in self.pad_records[pad_id].keys():
        if key != "used":
          fp.write("    <%s>%s</%s>\n" % \
                   (key, self.pad_records[pad_id][key], key))
      fp.write("  </pad-record>\n")
    fp.write("</onetime-pad-records>\n")
    fp.close()
    # On some operating systems, renaming a file onto an existing file
    # doesn't just silently overwrite the latter -- according to
    # https://github.com/kfogel/OneTime/issues/13, Microsoft Windows
    # will throw an error, for example.  So we do this rename very
    # carefully, and in such a way as to not to destroy any pad
    # records that might be left over from a past failed rename.
    intermediate_tempfile = self.pad_records_file + ".int"
    if os.path.exists(intermediate_tempfile):
      raise ConfigurationError(
        "Leftover intermediate pad-records file found;"
        "please sort things out:\n"
        "  %s" % intermediate_tempfile)
    os.rename(self.pad_records_file, intermediate_tempfile)
    os.rename(tempfile, self.pad_records_file)
    os.remove(intermediate_tempfile)

  def register(self):
    """Register this session's pad if it is not already registered, and
    set its offset based on previously used regions for that pad, if any."""
    next_offset = None
    # This is a little complicated only because we need; to look for
    # old original-style pad IDs and upgrade them if present.
    if not self.pad_records.has_key(self._pad_session.id()):
      if self.pad_records.has_key(
          self._pad_session.id(format_level="original")):
        # Upgrade original-style record to internal style.
        self.pad_records[self._pad_session.id()] \
          = self.pad_records[self._pad_session.id(format_level="original")]
        del self.pad_records[self._pad_session.id(format_level="original")]
      else:
        # Initialize a new internal-style record.
        self.pad_records[self._pad_session.id()] = { "used" : [ ] }
    else:
      if self.pad_records.has_key(
          self._pad_session.id(format_level="original")):
        raise Configuration.ConfigurationError(
          "Pad has both v2 and v1 IDs present in pad-records file:\n" \
          "  v2: %s\n"                                                \
          "  v1: %s\n"                                                \
          "This is supposed to be impossible.  Please resolve."       \
          % (self._pad_session.id(),
             self._pad_session.id(format_level="original")))
    # One way or another, we now have an up-to-date v2 pad record.
    # Set the next offset accordingly.
    next_offset = self._get_next_offset(
      self.pad_records[self._pad_session.id()]["used"])
    self._pad_session.set_offset(next_offset)

  def record_consumed(self, allow_reconsumption):
    """Record pad ranged currently used by self._pad_session.

    If ALLOW_RECONSUMPTION is False, raise a ConfigurationError
    if reconsuming any part of a range that has been consumed previously.
    But if ALLOW_RECONSUMPTION is True, allow ranges to overlap.
    Typically, it is False when encrypting and True when decrypting,
    because it's okay to decrypt a message multiple times, but not to
    re-use a range for encrypting."""
    used = self.pad_records[self._pad_session.id()]["used"]
    used.append((self._pad_session.offset(), self._pad_session.length()))
    self.pad_records[self._pad_session.id()]["used"] \
      = self._consolidate_used_ranges(used, allow_reconsumption)

  def show_pad_records(self):
    """Print pad records, presumably for debugging."""
    for pad_id in self.pad_records.keys():
      print "PadSession %s:" % pad_id
      print "  used:", self.pad_records[pad_id]["used"]


class RandomEnough:
  """Class for providing [pseudo]random bytes that are XOR'd against pad.

          **************************************************
          ***                   NOTE:                    ***
          ***                                            ***
          ***  DON'T USE THIS FOR ENCRYPTING PLAINTEXT.  ***
          ***  THAT IS NOT WHAT IT IS FOR.  JUST DON'T.  ***
          ***                                            ***
          **************************************************

  There are a couple of places (the head fuzz and tail fuzz) where raw
  pad bytes would otherwise be exposed in the encrypted message,
  except that they're not exposed because they're XOR'd with bytes
  coming from this class.  That's all this class is for.

  If the pad is truly random, as it should be, then the randomness
  produced by this class is irrelevant.  If the pad is not truly
  random, then that's tragic, but this class at least helps disguise
  that fact a bit.  Still, it's just a "best effort" kind of thing.

  In any case, these bytes are *never* to be used as a fallback
  replacement for missing pad data, obviously.  They're just about
  making the fuzz fuzzier; they have nothing to do with real data.

  If TEST_MODE, then use pseudo-random numbers with a defined seed,
  so that the same stream of random numbers is always produced.

  """
  def __init__ (self, test_mode=False):
    """Initialize, optionally with integer SEED."""
    if test_mode:
      random.seed(1729)
  def rand_bytes(self, num):
    """Return NUM random bytes."""
    try:
      # TODO: Right now we offer pseudo-random bytes based on whatever
      # random seed Python is using (unless test_mode, in which case
      # the seed is predefined).  Even though the random bytes
      # produced here are not essential to the security of OneTime's
      # output, still it would be best if they were as random as we
      # could make them.  We could use a random.SystemRandom object
      # here if one is available, but the Python documentation says
      # that class is not supported on all systems, while not saying
      # what error is raised if it's not supported.  (Maybe one is
      # just supposed to check directly for it in random.__dict__?)
      raise NotImplementedError("just testing")
      return os.urandom(num)
    except NotImplementedError:
      ret_data = bytearray(b'\x00' * num)
      for i in range(num):
        ret_data[i] = chr(random.randint(0, 255))
      return str(ret_data)

class PadSession:
  """An encrypter/decrypter associated with a pad at an offset.
Feed bytes through convert() to XOR them against that pad.

A PadSession is used for a single encryption or decryption session; it
should not be used for subsequent sessions -- instead, a new PadSession
object should be generated (it might refer to the same underlying pad file,
but it still needs to be a new object due to certain initializations)."""

  # Length of the front stretch of pad used for the ID.
  _id_source_length = 32

  # The plaintext is authenticated with a SHA256 hash digest
  # (computed in self._session_hash) that is itself encrypted with the
  # pad and included in the ciphertext.  This is the length of that
  # digest.  Note it is the length of the raw digest, not the length
  # of a hexadecimal representation of the digest.
  _digest_length = 32

  # Number of contiguous raw pad bytes to use as the source
  # material for the digest computed in self._session_hash.
  _digest_source_length = 32

  def __init__(self, pad_path, config_area=None,
               no_trace=False, test_mode=False):
    """Make a new pad session, with padfile PAD_PATH.
    The pad session cannot be used for encrypting or decrypting until
    set_offset() is called.  If CONFIG_AREA is not None, it is the
    directory containing the pad-records file.  If NO_TRACE, then
    don't make any changes in the configuration area.  If TEST_MODE,
    then use pseudo-random numbers with a defined seed, so that output
    is always the same when the input is the same."""
    self.pad_path = pad_path
    self.config = Configuration(self, config_area)
    self._no_trace = no_trace
    self.padfile = open(self.pad_path, "rb")
    self.pad_size = os.stat(self.pad_path)[stat.ST_SIZE]
    self._offset = None  # where to start using pad bytes (must initialize)
    self._length = 0  # number of pad bytes used this time
    self._default_fuzz_source_length = 2   # See _get_fuzz_length_from_pad()
    self._default_fuzz_source_modulo = 512 # and see _make_inner_header().
    self._randy = RandomEnough(test_mode)

    # These are just caches for self.id(), which see.
    self._id = None
    self._original_format_level_id = None

    # If this session saw a particular format level, record it so we
    # can check that it remains consistent.
    self._format_level = None

    # On decrypting, a given call to convert() might not supply enough
    # string to use up the inner headers.  Therefore we must remember
    # how much of the head_fuzz still needs to be used up.
    self._fuzz_remaining_to_consume = 0

    # We compute a hash of the plaintext head fuzz + plaintext message
    # and embed that hash into the encrypted text, for authentication
    # of the overall encrypted message.  See self._initialize_hash().
    self._session_hash = None

    # There are both head and tail fuzz, but we only need to remember
    # the tail fuzz length, because we learn that length at the start
    # of processing but wait till the end to emit or consume it --
    # whereas head fuzz we emit/consume as soon as we know its length.
    self._tail_fuzz_length = None
    self._tail_fuzz_length_source_bytes = None

    # This buffer holds all encrypted head fuzz bytes that have not
    # yet been emitted by convert().  That is, when this buffer is not
    # empty, then it is what convert() needs to emit *before* it emits
    # anything else -- the first part of the output of the first call.
    self._head_buffer = ''

    # This buffer always holds the latest input, and must always be at
    # least as long as the digest + tail fuzz, so that we can
    # refrain from decrypting them as part of the original plaintext.
    self._tail_buffer = ''

    # Most of what a pad session does is the same for encrypting and
    # decrypting -- after all, the conversion step is symmetrical (XOR).
    #
    # However, before conversion can happen, the pad session needs to know
    # whether to write or read the inner header flag bytes -- so for
    # that it needs to know whether it's encrypting or decrypting.  When
    # that step is done, the appropriate variable below is set;
    # exactly one of them *must* be set before any conversion happens.
    self._encrypting = False
    self._decrypting = False
    # False until conversion starts, True thereafter.  (Conversion
    # starts after all the head fuzz has been consumed.)
    self._begun = False

    # Register with config as last thing we do.
    self.config.register()

  class PadSessionUninitialized(Exception):
    "Exception raised if PadSession hasn't been initialized yet."
    pass

  class OverPrepared(Exception):
    "Exception raised if a PadSession is initialized or prepared twice."
    pass

  class PadShort(Exception):
    "Exception raised if pad doesn't have enough data for this encryption."
    pass

  class FormatLevel(Exception):
    "Exception raised for an unknown or inconsistent format level."
    pass

  class InnerFormat(Exception):
    "Exception raised if something is wrong with the inner format."
    pass

  class FuzzMismatch(Exception):
    "Exception raised if the amount of tail fuzz is incorrect."
    # In practice this error can never happen for head fuzz, because
    # if the length of the head fuzz is wrong, the digest will not
    # match either, and we'll catch the digest error first.
    pass

  class DigestMismatch(Exception):
    "Exception raised if a digest check fails."
    pass

  def _initialize_hash(self):
    """Initialize the session hash with some raw pad bytes."""
    if self._offset + self._digest_source_length >= self.pad_size:
      raise PadSession.PadShort(
        "digest initialization failed because pad too short")
    digest_source_bytes = self.padfile.read(self._digest_source_length)
    self._length += self._digest_source_length
    if self._session_hash is not None:
      raise PadSession.OverPrepared(
        "pad session hash was prematurely initialized")
    self._session_hash = hashlib.sha256()
    self.digest_gulp(digest_source_bytes)

  def prepare_for_encryption(self):
    """Mark this PadSession as encrypting.  This or prepare_for_decryption()
    must be called exactly once, before any conversion happens."""
    if self._encrypting:
      raise PadSession.OverPrepared("already prepared for encryption")
    if self._decrypting:
      raise PadSession.OverPrepared(
        "cannot prepare for both encryption and decryption")
    self._head_buffer = self._make_inner_header()
    self._encrypting = True

  def prepare_for_decryption(self):
    """Mark this PadSession as encrypting.  This or prepare_for_encryption()
    must be called exactly once, before any conversion happens."""
    if self._decrypting:
      raise PadSession.OverPrepared("already prepared for decryption")
    if self._encrypting:
      raise PadSession.OverPrepared(
        "cannot prepare for both decryption and encryption")
    # The fact that we don't call self._handle_inner_header() here is
    # an unfortunate asymmetry w.r.t. self.prepare_for_encryption().
    # The reason for it is that the decryption code flow is
    # complicated by the need to handle remainder input, in a way that
    # encryption is not.  This is why self._handle_inner_header() has
    # to be called from self.convert().
    self._decrypting = True

  def set_offset(self, offset):
    """Set this pad session's encryption/decryption offset to OFFSET."""
    if offset >= self.pad_size:
      raise PadSession.PadShort("offset exceeds pad size, need more pad")
    self._offset = offset
    self.padfile.seek(self._offset)

  def convert(self, string, format_level="internal"):
    """If STRING is not empty or None, return it as XORed against the pad;
else return the empty string.  Note STRING may be empty on intermediate
calls simply because a compressor has not yet had enough incoming data to
work with, not necessarily because input is ended yet.

If FORMAT_LEVEL is "original", then don't handle the head and tail
used by the later format levels.  Otherwise, do handle the head and
tail: for the head, consume over the fuzz and include it in the
overall message digest; for the tail, just remember its length so we
can consume it later.

It is an error to call this multiple times with different FORMAT_LEVEL
values, for a given PadSession instance.  Whatever you pass the first time
must be used for all subsequent calls with that instance.

    """
    result = ''
    if self._offset is None:
      raise PadSession.PadSessionUninitialized(
        "pad session not yet initialized (no offset)")
    if self._format_level is None:
      self._format_level = format_level
    elif self._format_level != format_level:
      raise PadSession.FormatLevel(
        "inconsistent format levels requested: '%s' and '%s'"
        % (self._format_level, format_level))
    if format_level == "internal":
      if self._encrypting and self._decrypting:
        raise PadSession.OverPrepared(
          "pad session cannot encrypt and decrypt simultaneously")
      elif not self._encrypting and not self._decrypting:
        raise PadSession.PadSessionUninitialized(
          "pad session not yet prepared for either encrypting or decrypting")
      elif not self._begun:
        if self._decrypting:
          # In the decryption case, the only way we receive any head
          # fuzz material is during the initial call(s) to convert().
          # So here we check for that and make sure to consume all the
          # head fuzz before continuing on to regular decryption.  In
          # theory, this could involve multiple calls to convert,
          # although in practice it always seems to get done during
          # the first call.
          #
          # TODO (minor): It's possible string might be so short that
          # it doesn't even contain enough information to know the fuzz
          # length yet.  The solution is easy: if we haven't yet begun,
          # then just accumulate string to prepend to the next call(s),
          # until a call comes when we have enough to work with.
          #
          # This is not an urgent problem, as in practice no I/O system
          # is likely to deliver string in chunks so small.  So, saving
          # it to solve later.

          # The complement of this call to self._handle_inner_header()
          # is located in self._prepare_for_encryption() in the
          # encryption case, which also prepares self._head_buffer for
          # the first call to convert().  However, the decryption case
          # is complicated by the need to return remainder
          # information, in a way that the encryption case is not.
          # This asymmetry is reflected in the code.
          string, fuzz_remaining = self._handle_inner_header(string)
          if string != "" and fuzz_remaining != 0:
            raise PadSession.InnerFormat(
              "Got both a result string and a pad remainder")
          if fuzz_remaining > 0:
            self._fuzz_remaining_to_consume = fuzz_remaining

      if self._fuzz_remaining_to_consume > 0:
        new_fuzz_remaining = 0
        num_bytes_to_consume_now = None
        if self._fuzz_remaining_to_consume > len(string):
          new_fuzz_remaining = self._fuzz_remaining_to_consume - len(string)
          num_bytes_to_consume_now = len(string)
        else:
          num_bytes_to_consume_now = self._fuzz_remaining_to_consume
        self._fuzz_remaining_to_consume = new_fuzz_remaining
        num_fuzz_bytes_remaining, string = self._consume_fuzz_bytes(
          num_bytes_to_consume_now, string, is_head_fuzz=True)
        self._fuzz_remaining_to_consume += num_fuzz_bytes_remaining

      # Once we've handled any inner headers, buffer for decrypting.
      if self._decrypting:
        self._tail_buffer += string
        # Reserve exactly the tail length each time, so that on the
        # last iteration we can just check both parts of the tail
        # (the digest and the fuzz) without emitting new output.
        if len(self._tail_buffer) < (PadSession._digest_length
                                     + self._tail_fuzz_length):
          string = ''   # wait until we have more or are done
        else:
          string = self._tail_buffer[:(0 - (PadSession._digest_length
                                            + self._tail_fuzz_length))]
          self._tail_buffer = self._tail_buffer[
            (0 - (PadSession._digest_length + self._tail_fuzz_length)):]

    string_len = len(string)
    pad_str = self.padfile.read(string_len)
    if len(pad_str) < string_len:
      raise PadSession.PadShort(
        "not enough pad data available to finish encryption")
    for i in range(string_len):
      result += chr(ord(string[i]) ^ ord(pad_str[i]))
    self._length += string_len
    self._begun = True
    if self._head_buffer:
      # In the encryption case, we generated the head fuzz entirely
      # internally during the preparation stage, and just buffered it
      # for prepending during the first call to convert().  So if
      # we're here, then it must be the first call to convert() when
      # encrypting, and it's time to use and empty that buffer.
      #
      # (The decryption case is not quite symmetrical.  We can't
      # consume the head fuzz during the preparation stage, because at
      # that point we haven't received any of the input yet -- the
      # only route for receiving input is via calls to convert().  So
      # the complement of this code, in the decryption case, is the
      # handling of head fuzz before the self._begun flag is set.)
      result = self._head_buffer + result
      self._head_buffer = ''
    return result

  def _get_id(self):
    """Get the ID for this session's underlying pad.
    (The ID is just the pad's first 32 bytes in hexadecimal.)"""
    # The astute reader may ask: why are we bothering to make a 32
    # byte hash of 32 bytes worth of random data, instead of just
    # using the data itself (expressed in hexadecimal) as the pad ID?
    # The answer is just tradition, really.  Well, and the very slight
    # possibility that if there's *something* not quite random about
    # the pad, even though that's bad, we can at least avoid revealing
    # that fact by exposing the first 32 bytes of the pad.  If a
    # pad-records file gets leaked, that shouldn't show anything of
    # interest about the pad itself, only about how much the pad has
    # been used.
    saved_posn = self.padfile.tell()
    self.padfile.seek(0)
    sha256 = hashlib.sha256()
    string = self.padfile.read(PadSession._id_source_length)
    sha256.update(string)
    self.padfile.seek(saved_posn)
    return sha256.hexdigest()

  def _get_original_format_level_id(self):
    """Get the OneTime \"original\" format level ID for the session pad.
    In that format level, pad IDs were based on the first 1024
    (octet) bytes of the pad.  This was needlessly spendy, or rather,
    it would have been needlessly spendy if OneTime 1.x had been
    paranoid enough to not use any of those bytes for encryption.
    Version 2.0 fixed this, reducing the number of bytes used on ID but
    also making they are not used for encryption."""
    saved_posn = self.padfile.tell()
    self.padfile.seek(0)
    sha1 = hashlib.sha1()
    string = self.padfile.read(1024)
    sha1.update(string)
    self.padfile.seek(saved_posn)
    return sha1.hexdigest()

  def id(self, format_level="internal"):
    """Return the pad ID of the pad belonging to this pad session.
    If FORMAT_LEVEL is specified, return ID according to that level."""
    if format_level == "internal":
      if self._id is None:
        self._id = self._get_id()
      return self._id
    elif format_level == "original":
      if self._original_format_level_id is None:
        self._original_format_level_id = self._get_original_format_level_id()
      return self._original_format_level_id
    else:
      raise PadSession.FormatLevel("unknown format \"%s\" for ID"
                                   % format_level)

  def path(self):
    """Return the pad's path."""
    return self.pad_path

  def offset(self):
    """Return offset from which encryption/decryption starts."""
    return self._offset

  def length(self):
    """Return the number of pad bytes used so far."""
    return self._length

  def _get_fuzz_length_from_pad(self, num_bytes, modulo):
    """Calculate a fuzz length based on the next NUM_BYTES % MODULO,
    advancing the pad accordingly.  Return that length and the raw pad
    data used to calculate it in a tuple of the form:

      [calculated_length, source_bytes]
    """
    # What's going on here?  What is "fuzz"?
    #
    # "Fuzz" is some random data that pads the plaintext+digest on
    # either side; the fuzz in front is "head fuzz" and the fuzz at
    # the end is "tail fuzz".  Fuzz consists of a random length of
    # random bytes -- the length is computed from pad, the bytes
    # themselves are generated randomly at run time), and XOR'd
    # against pad -- such the position of the plaintext+digest is not
    # known even to an attacker who can see the pad-records file.
    #
    # That is, because the length of the fuzz is based on data in the
    # pad, and a corresponding amount of pad is consumed, the position
    # of the fuzz is known only to those who have the pad, and the
    # actual content of the fuzz is either perfectly random (if the
    # pad is perfectly random) or pretty darned random (if the pad is
    # only pseudo-random, which would be very bad, but at least
    # OneTime tries to mitigate the situation as much as it can and
    # not leak any information about raw pad data).
    #
    # For both head fuzz and the tail fuzz, the fuzz length may be
    # determined in two ways:
    #
    #   1) Read NUM_BYTES from the current location in the pad, and
    #      use those bytes to calculate (in some deterministic way)
    #      the fuzz length to use.
    #
    #   2) Read a sender-specified length that is encrypted in the
    #      pad right here ("here" being right after the inner header
    #      bytes), and then use that length.
    #
    # Right now we only use method (1), but support for method (2) is
    # built into the inner header format; see _make_inner_header().
    #
    # Here's how method (1) works:
    #
    # First we read NUM_BYTES bytes from the pad (the bytes are
    # consumed -- they won't be used for encrypting or decrypting
    # data).
    #
    # We then portably convert that sequence of bytes to a number
    # modulo MODULO.  The result is the number of bytes of fuzz.
    #
    # See _make_inner_header() for why we do things this way.
    fuzz_length_source = self.padfile.read(num_bytes)
    self._length += num_bytes
    if len(fuzz_length_source) < num_bytes:
      raise PadSession.PadShort(
        "not enough pad available to supply fuzz length source bytes")
    fuzz_length = 1
    for x in fuzz_length_source:
      # We don't use int.from_bytes(), as it's only available in
      # Python >= 3.2.  Instead, we just multiply the bytes together
      # (as eight-bit values) and take the modulo of that.  It would
      # be more space-efficient to convert multiple bytes together as
      # a word-sized number of some kind, but that's harder to do
      # portably, and this has to work everywhere consistently.
      #
      # Since the current modulo is 512 and the default fuzz source
      # length is 2, we could have just done
      #
      #   sum([ord(x) for x in fuzz_length_source]) % 512
      #
      # But we may want to increase modulo and/or default fuzz source
      # length later; multiplying all the bytes together is as
      # efficient as we can be while maintaining portability.
      fuzz_length *= ord(x)
    return [fuzz_length % modulo, fuzz_length_source]

  def _consume_fuzz_bytes(self, num_bytes, string, is_head_fuzz=False):
    """Consume as much of the next NUM_BYTES of fuzz from STRING as possible.
    STRING is a window of source material that starts with fuzz, but
    may continue on into non-fuzz.  If STRING is shorter than NUM_BYTES,
    consume as much as possible.

    Return a tuple of [num_fuzz_bytes_remaining, unconsumed_string].
    If all the fuzz has been consumed, the first element will be 0,
    and the second element will be a string (though possibly of zero
    length, if the fuzz ended exactly with the end of STRING).  If
    there is still fuzz left to be consumed, but STRING wasn't long
    enough for us to reach the remainder of the fuzz, then the first
    element will be the amount of fuzz left -- that is, the amount of
    fuzz that must still be consumed in future calls with new STRING
    arguments -- and the second element will definitely be a string of
    zero length, because we consumed all of STRING to get as much fuzz
    as possible in this call.

    If IS_HEAD_FUZZ is true, update the session hash from the
    NUM_BYTES of data read."""
    try:
      # The max amount we can actually consume in this call.
      consumable_len = min(len(string), num_bytes)
      pad_data = self.padfile.read(consumable_len)
      self._length += consumable_len
      if is_head_fuzz:
        hash_input = ""
        for i in range(consumable_len):
          hash_input += chr(ord(string[i]) ^ ord(pad_data[i]))
        self.digest_gulp(hash_input)
      num_bytes_remaining = max(0, num_bytes - len(string))
      return [num_bytes_remaining, string[consumable_len:]]
    except EOFError:
      raise PadSession.PadShort("not enough pad available to match fuzz")

  def _make_fuzz(self, num_bytes, is_head_fuzz=False):
    """Return NUM_BYTES worth of encrypted fuzz data, advancing the pad.
If IS_HEAD_FUZZ is true, then update the session hash with the NUM_BYTES
of random fuzz data generated herein."""
    try:
      rnd_data = self._randy.rand_bytes(num_bytes)
      pad_data = self.padfile.read(num_bytes)
      # ret_data = bytearray(b'\x00' * num_bytes)
      ret_data = ''
      self._length += num_bytes
      for i in range(num_bytes):
        ret_data = ret_data + chr(ord(rnd_data[i]) ^ ord(pad_data[i]))
      if is_head_fuzz:
        self.digest_gulp(rnd_data)
    except EOFError:
      raise PadSession.PadShort(
        "not enough pad available to supply fuzz data")
    return ret_data

  def _make_inner_header(self):
    """Return inner header data to be embedded in the output.
    This must happen before any conversion of plaintext to ciphertext
    is done, so it must be called after the PadSession has been initialized
    but before self.convert() has consumed any pad for actual conversion."""
    inner_header = ''
    if self._offset is None:
      raise PadSession.PadSessionUninitialized(
        "pad session not yet initialized (no offset)")
    # We first jump to the offset specified by the plaintext headers.
    #
    # Then we generate the inner header bytes: a series of bytes
    # that indicate various things about the encryption of the plaintext.
    # The inner header bytes are themselves encrypted against the pad,
    # or (for the portion of the inner headers comprising the fuzz) are
    # random bytes encrypted against pad, so for each byte of inner
    # header we have to consume a byte of pad.
    #
    # The (plaintext) format of the inner headers is given below.
    # Some bytes are flag bytes, where each bit gives flags for
    # various options.  Depending on what those flags say, some of
    # the subsequent bytes indicate length or have other meanings.
    #
    #   BYTE 1:
    #     The first byte indicates the internal format version --
    #     think of it as the "x" in "internal.x", where "internal"
    #     comes from the plaintext "Format:" header.  Values 0-127 are
    #     interpreted as numbers; values > 128 mean combine this byte
    #     with the next byte (recursively, big endian).  The internal
    #     format version will probably never get that high, of course,
    #     but if it does, we're prepared :-).
    #
    #   BYTE 2:
    #     In internal format 0, the second byte holds flag bits:
    #
    #       0b_______*: 0 means use the default fuzz length
    #                   1 means next byte(s) hold sender-chosen fuzz length
    #       0b______*_: reserved; must be 0 in internal format 0
    #       0b_____*__: reserved; must be 0 in internal format 0
    #       0b____*___: reserved; must be 0 in internal format 0
    #       0b___*____: reserved; must be 0 in internal format 0
    #       0b__*_____: reserved; must be 0 in internal format 0
    #       0b_*______: reserved; must be 0 in internal format 0
    #       0b*_______: reserved; must be 0 in internal format 0
    #
    #     If the first bit were set, then the next byte would indicate
    #     something about the fuzz length.  That might either be a
    #     sender-chosen definite length (still masked by the pad of
    #     course), or a length partly determined by the pad (similarly
    #     to the current code below) but with a sender-chosen minimum.
    #     That's all TBD: we don't support internal format 1 yet, only
    #     0, and in 0 the fuzz lengths come entirely from pad data.
    #
    #     The meanings of the rest of the flag bits are not yet
    #     determined, and their values must be zero in format 2.0.
    #     (The value of the first bit must be zero right now, too,
    #     since sender-chosen lengths are not yet implemented, but at
    #     least we know more or less what that bit means already.)
    next_pad_byte = ord(self.padfile.read(1))
    self._length += 1
    # First byte is inner format version (currently 0):
    inner_header_format_version = 0 ^ next_pad_byte
    next_pad_byte = ord(self.padfile.read(1))
    self._length += 1
    # Next byte indicates how to determine fuzz_source length:
    fuzz_source_length_indicator = 0 ^ next_pad_byte
    # Now that we've taken care of the start of the header, we can
    # figure out how much head fuzz to use.
    #
    # You may be wondering, why do we have fuzz at all?
    #
    # By prepending and appending random amounts of data (the head
    # fuzz and tail fuzz), we make it impossible for the attacker to
    # know exactly where the real encrypted text starts and ends or
    # how long the encrypted text is.
    #
    # This means she can't derive the relevant part of the pad, so she
    # can't substitute another plaintext, even if she knows the
    # original plaintext; and if she doesn't know the original
    # plaintext, she has no way to derive the length of the plaintext
    # from the encrypted text.
    #
    # The fuzz isn't the only thing preventing substitution, because
    # encrypted messages are also authenticated with checksums made
    # from both pad data and plaintext data.  But this way an attacker
    # has only a "1 / fuzz_length" chance of even guessing where in
    # the message the compressed plaintext *is*, regardless of whether
    # she has a known plaintext -- the tail fuzz distance means she
    # can't just count backwards from the end of the ciphertext.
    #
    # We can't afford to disguise the position or length by very much,
    # because pad isn't cheap and we don't want to use it up on fuzz.
    # Hence, self._default_fuzz_source_modulo is currently 512.  We
    # will read two bytes two determine the head fuzz distance (i.e.,
    # the head fuzz distance will be 0 <= N <= 511), and read the next
    # two bytes to determine the tail fuzz distance (ditto).
    #
    # It's tempting to set a higher modulus than 512, but given the
    # rate at which I at least can generate new pad data, that feels
    # too costly.  512 feels like the right point in the tradeoff
    # slider.  If anyone has data to support a different number, I
    # hope they'll speak up.  With self._default_fuzz_source_length at
    # two bytes, we could go all the way up to 65535, so most of this
    # code can stay the same if we later decide to expand the range.
    # (There's no particular reason the modulus has to be a power of
    # two, either, other than good taste.)
    #
    # Remember that the 512 should not be thought of as a key length.
    # The number of possibilities here is 1-in-512, not 1-in-(2^512).
    #
    # A possible improvement would be to fuzz by bits instead of
    # bytes, so we get 8 times the variability for the amount of pad
    # used up.  But fuzzing by bits would complicate the code, since
    # all the seeking and I/O is natively aligned on 8-bit boundaries,
    # and inspectable simplicity is one of OneTime's goals.
    head_fuzz_length, head_fuzz_length_source_bytes \
      = self._get_fuzz_length_from_pad(
        self._default_fuzz_source_length, self._default_fuzz_source_modulo)
    self._tail_fuzz_length, self._tail_fuzz_length_source_bytes \
      = self._get_fuzz_length_from_pad(
        self._default_fuzz_source_length, self._default_fuzz_source_modulo)
    self._initialize_hash()
    head_fuzz = self._make_fuzz(head_fuzz_length, is_head_fuzz=True)
    result = chr(inner_header_format_version)       \
             + chr(fuzz_source_length_indicator)    \
             + head_fuzz_length_source_bytes        \
             + self._tail_fuzz_length_source_bytes  \
             + head_fuzz
    return result

  def _handle_inner_header(self, string):
    """Handle inner header data at front of STRING.
    Return remainder of STRING or the length left to treat as fuzz (if
    STRING is shorter than the inner headers), as a tuple:

      [string_remainder_if_any, fuzz_length_remaining_if_any]

    The tuple's two elements mutually exclude: if the string remainder
    is not the empty string, then the fuzz remainder length must be 0;
    else the fuzz remainder length must be an integer > 0 and the
    string remainder must be the empty string.

    This all must happen before any conversion of ciphertext to plaintext
    is done, so it must be called after the PadSession has been initialized
    but before self.convert() has consumed any pad for actual conversion.

    """
    # See self._make_inner_header() for inner header documentation.
    inner_format_version = None
    fuzz_length = 0
    if self._offset is None:
      raise PadSession.PadSessionUninitialized(
        "pad session not yet initialized (no offset)")
    next_pad_byte = ord(self.padfile.read(1))
    self._length += 1
    fuzz_length += 1
    pad_encrypted_byte = ord(string[0])
    inner_format_version = pad_encrypted_byte ^ next_pad_byte
    if inner_format_version == 0:
      first_flag_byte = ord(string[1]) ^ ord(self.padfile.read(1))
      self._length += 1
      fuzz_length += 1
      if first_flag_byte & 1 == 0:
        head_fuzz_length, head_fuzz_length_source_bytes \
          = self._get_fuzz_length_from_pad(
            self._default_fuzz_source_length,
            self._default_fuzz_source_modulo)
        self._tail_fuzz_length, self._tail_fuzz_length_source_bytes \
          = self._get_fuzz_length_from_pad(
            self._default_fuzz_source_length,
            self._default_fuzz_source_modulo)
        self._initialize_hash()

        num_fuzz_bytes_remaining, unconsumed_string \
          = self._consume_fuzz_bytes(
            head_fuzz_length,
            string[2 + (self._default_fuzz_source_length * 2):],
            is_head_fuzz=True)
        # TODO: We're capturing these return values above but then
        # never using them, except for debugging.  That seems odd.
        # Check with later return of remaining amount to see if at
        # least there's an assertion that could be checked.
        #
        # If you print them out by putting this code here...
        #
        #   sys.stderr.write("DBG: nfzzbr %d, uncnsm_str %d\n"
        #                    % (num_fuzz_bytes_remaining, len(unconsumed_string)))
        #   sys.stderr.flush()
        #
        # ...and then run 'make check', it shows that the unconsumed
        # string is always zero while num_fuzz_bytes_remaining varies:
        #
        #   DBG: nfzzbr 344, uncnsm_str 0
        #   PASS: basic encryption, decryption
        #   DBG: nfzzbr 344, uncnsm_str 0
        #   PASS: encryption, decryption of large plaintext
        #   DBG: nfzzbr 39, uncnsm_str 0
        #   DBG: nfzzbr 409, uncnsm_str 0
        #   DBG: nfzzbr 157, uncnsm_str 0
        #   DBG: nfzzbr 14, uncnsm_str 0
        #   DBG: nfzzbr 297, uncnsm_str 0
        #   PASS: option parsing
        #   PASS: failed decryption should give an error and create no output
        #   DBG: nfzzbr 54, uncnsm_str 0
        #   PASS: decryption should not shrink pad usage
        #   DBG: nfzzbr 54, uncnsm_str 0
        #   PASS: decryption should record same pad usage as encryption
        #   DBG: nfzzbr 54, uncnsm_str 0
        #   DBG: nfzzbr 54, uncnsm_str 0
        #   DBG: nfzzbr 155, uncnsm_str 0
        #   DBG: nfzzbr 39, uncnsm_str 0
        #   DBG: nfzzbr 39, uncnsm_str 0
        #   DBG: nfzzbr 409, uncnsm_str 0
        #   PASS: test reconsumption via repeated encoding and decoding
        #   PASS: make sure '--show-id' shows everything it should
        #   PASS: same plaintext should encrypt smaller with v2+ than with v1
        #   PASS: decode v1 msg, where v1 entry has range already used
        #   PASS: decode v1 msg, where v1 entry has range not already used
        #   DBG: nfzzbr 344, uncnsm_str 0
        #   PASS: decode v2 msg, where v1 entry has range already used
        #   DBG: nfzzbr 344, uncnsm_str 0
        #   PASS: decode v2 msg, where v1 entry range needs stretching
        #   DBG: nfzzbr 344, uncnsm_str 0
        #   PASS: decode v2 msg, where v1 entry needs new range
        #   PASS: decode v1 msg, where no entry in pad-records at all
        #   PASS: encode msg, where v1 pad entry has some range already used
        #   PASS: decode msg, erroring because garbage after base64 data
        #   PASS: tampered head fuzz is detected, but decryption succeeds
        #   PASS: tampering with ciphertext causes bzip decoder error
        #   DBG: nfzzbr 344, uncnsm_str 0
        #   PASS: basic encryption/decryption with all-nulls plaintext
        #   PASS: tampering with tail fuzz should have no effect
        #   PASS: basic encryption/decryption with zero-length tail fuzz
        #   PASS: tampering with message digest causes authentication error
        #   PASS: tampering with head fuzz causes authentication error
        #
        # It seems clear there's a latent bug here.  We are counting
        # on there always being enough head fuzz to carry over into
        # upcoming methods that will consume more input.  If there
        # happened to be small enough head fuzz to make this not be
        # the case, then suddenly things might break.  This is dumb.
        # It's also testable, with the right hand-constructed pad.

        fuzz_length += (self._default_fuzz_source_length * 2) \
                          + head_fuzz_length
      else:
        raise PadSession.InnerFormat(
          "cannot yet handle custom fuzz_source length")
      # Enforce the fact that we don't use any of the other flag bits yet.
      if (   first_flag_byte &   2 != 0
          or first_flag_byte &   4 != 0
          or first_flag_byte &   8 != 0
          or first_flag_byte &  16 != 0
          or first_flag_byte &  32 != 0
          or first_flag_byte &  64 != 0
          or first_flag_byte & 128 != 0):
        raise PadSession.InnerFormat(
          "first flag byte has unknown flags set (%s)"
          % bin(first_flag_byte))
    else:
      raise PadSession.InnerFormat("unknown inner format version \"%d\""
                            % inner_format_version)
    amount_left = 0;
    # TODO: Check above TODO against these calculations:
    if len(string) < fuzz_length:
      amount_left = fuzz_length - len(string);
      return ["", amount_left];
    else:
      return [string[fuzz_length:], 0]

  def digest_gulp(self, string):
    """Incorporate STRING into the running digest for this session."""
    self._session_hash.update(string)

  def _verify_digest(self):
    """Verify that the head fuzz + message digest embedded at the current
    location matches the expected digest in self._session_hash,
    consuming PadSession._digest_length bytes of pad in the process.
    If any mismatch, raise a PadSession.DigestMismatch error showing
    the two digests in hexadecimal format."""
    pad_bytes = self.padfile.read(PadSession._digest_length)
    self._length += PadSession._digest_length
    received_digest = ''
    for i in range(PadSession._digest_length):
      received_digest += chr(ord(self._tail_buffer[i]) ^ ord(pad_bytes[i]))
    # Advance the tail buffer past the part we've just consumed.
    self._tail_buffer = self._tail_buffer[PadSession._digest_length:]
    # Finally, verify the digests.
    if self._session_hash.digest() != received_digest:
      raise PadSession.DigestMismatch(
        "digest mismatch:\n  computed: %s\n  received: %s\n"
        % (self._session_hash.hexdigest(), received_digest.encode('hex')))

  def finish(self):
    """Close out this pad session.  If this PadSession is at an
    \"internal\" (i.e., post-1.x) format level, then:

    If encrypting, return the pad-encrypted session digest, and the
    tail fuzz, as a single string.

    If decrypting, the return value is undefined and should be ignored,
    but raise a PadSession.DigestMismatch error if the session digest
    does not match the calculated digest, and raise PadSession.FuzzMismatch
    if the amount of tail fuzz found doesn't match the amount expected."""
    remainder = None
    if self._format_level == "internal":
      if self._tail_fuzz_length is None:
        raise PadSession.PadSessionUninitialized(
          "tail fuzz length never initialized")
      elif self._encrypting:
        remainder  = self.convert(self._session_hash.digest())
        remainder += self._make_fuzz(self._tail_fuzz_length)
      elif self._decrypting:
        # Decryption has already succeeded by the time we encounter
        # the tail, but if the tail doesn't match, we should still let
        # the user know the message was tampered with.
        #
        # There are two ways the tail could fail to match: either a
        # digest mismatch (i.e., an authentication failure), or a
        # mismatch against the expected tail fuzz bytes.
        #
        # Note that checking the tail has the important side effect
        # of recording that last bit of pad usage on decryption, which
        # is important for avoiding pad re-use -- that's the bug for
        # which a test was added in commit 9e8422f07.
        self._verify_digest()
        num_fuzz_bytes_remaining, unconsumed_string = self._consume_fuzz_bytes(
          self._tail_fuzz_length, self._tail_buffer)
        if num_fuzz_bytes_remaining != 0 or unconsumed_string != '':
          raise PadSession.FuzzMismatch("some source tail fuzz left over")
      else:
        raise PadSession.OverPrepared(
          "pad session out of whack: both encrypting and decrypting")
    # Record consumption last, after we've used the last bits of
    # pad we're going to use in this session.
    self.config.record_consumed(self._decrypting)
    if not self._no_trace: self.config.save()
    return remainder

  def __str__(self):
    """Return a string representation of this pad session."""
    return "PadSession '%s' (%s):\n   Offset: %d\n   Length: %d\n" \
           % (self.path(), self.id(), self.offset(), self.length())

class SessionEncoder:
  """Class for encoding raw data to OneTime output."""

  class UnknownCompressionException(Exception):
    """Exception raised when an unknown compression method is requested."""
    pass

  def __init__(self, pad_sess):
    """Initialize an encoder for PAD_SESS."""
    self.pad_sess = pad_sess
    # We use bz2 compression unconditionally.  If we offered a choice,
    # we'd have to name that choice somewhere for use in decryption.
    # But if we were to list the choice in the open headers, then we
    # would reveal something about the plaintext.  So we'd list it in
    # the inner headers, which would be fine, but it complicates the
    # code for no convincing reason: bz2 compression applied to
    # something non-compressible is more or less a no-op, give or take
    # a few bytes here and there.  Part of the point of this program
    # to be so simple as to be trivially auditable.  So we just use
    # bz2 unconditionally; the worst case is ok.
    self.compressor = bz2.BZ2Compressor()
    self.pad_sess.prepare_for_encryption()

  def encode(self, string):
    """Return onetime-encoded data for STRING, or the empty string if none.
    Consume pad as needed."""
    out = ''
    compressed_plaintext = self.compressor.compress(string)
    if compressed_plaintext:
      out = base64.encodestring(self.pad_sess.convert(compressed_plaintext))
    self.pad_sess.digest_gulp(string)
    return out

  def finish(self):
    "Return last remaining bits of ciphertext, or None if none left."
    remainder = self.pad_sess.convert(self.compressor.flush())
    remainder += self.pad_sess.finish()
    remainder = base64.encodestring(remainder)
    return remainder


class SessionDecoder:
  """Class for decoding OneTime output back to plaintext."""

  class DecodingError(Exception):
    """Exception raised when something goes wrong decoding."""
    pass

  def __init__(self, pad_sess, format_level):
    """Initialize a decoder for PAD_SESS.
    FORMAT_LEVEL indicates the OneTime format level of the incoming
    data, i.e., the value given in the output's "Format:" header."""
    self.pad_sess = pad_sess
    self.unused_data = ""
    self.format_level = format_level
    self.decompressor = bz2.BZ2Decompressor()

    if self.format_level != "original" and self.format_level != "internal":
      raise PadSession.FormatLevel(
        "impossible format level: \"%s\"" % self.format_level)
    self.pad_sess.prepare_for_decryption()

  def decode(self, string):
    """Return all available unreturned onetime-decoded data so far,
    including for STRING, or return None if no decoded data is ready yet.
    Throw IOError if data is not decodable.  Throw EOFError exception
    if called past the end of decodable data.  Store any unused data
    in self.unused_data."""
    ret = ""
    if self.format_level == "original":
      # Format level "original" got the compression/encryption order
      # wrong.  Look, this is embarrassing.  I'm only telling you
      # about it because we still need to support that mis-ordering
      # for compatibility.
      ret = self.pad_sess.convert((self.decompressor.decompress(
      base64.b64decode(string))), format_level="original")
    else: # must be format level "internal"
      ret = self.decompressor.decompress(self.pad_sess.convert(
        base64.b64decode(string)))
      self.pad_sess.digest_gulp(ret)
    self.unused_data += self.decompressor.unused_data
    return ret

  def finish(self):
    """Finalize this session's pad usage."""
    self.pad_sess.finish()


def license(outfile=sys.stdout):
  """Print open source license information to OUTFILE."""
  # Looks like we're maintaining this in parallel with the LICENSE
  # file.  I'd like to avoid that, but I don't see how.  The MIT
  # license text itself won't change, but the copyright years will
  # from time to time, and the copyright holder could as well.
  license_str = """\
OneTime version %s.

Copyright (c)  2004-2016  Karl Fogel

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
""" % __version__
  outfile.write(license_str)


def pad_generation_help(outfile=sys.stdout):
  """Print information on how to generate a pad."""
  help_str = """\
How to Generate a One-Time Pad
==============================

These commands will create a pad on computers that have a local source
of random bytes at /dev/random and the ability to copy those bytes
with the `dd` command.  Most GNU/Linux computers can do this.

  $ mkdir -p ~/.onetime
  $ dd if=/dev/random of=~/.onetime/to_foo.pad bs=1000 count=10000

That command will place a 10 megabyte pad in ~/.onetime/to_foo.pad
(1000000 bytes is one megabyte).  That pad will be good for encrypting
10 megabytes of data before it should be retired.  If you anticipate
sending more than 10 megabytes of data with the same pad, increase the
count accordingly.  On some computers, large pads may take a long
time, even days, to generate.  Just let dd work while you do other
things.  (Although you can go faster by using /dev/urandom instead of
/dev/random, opinion is divided on whether the random numbers from
/dev/urandom are good enough; to be safe, we recommend /dev/random,
but see http://www.2uo.de/myths-about-urandom/ for more discussion.)

To encrypt a file named WALLET.DAT with that pad:

  $ onetime -e -p ~/.onetime/to_foo.pad WALLET.DAT

(produces WALLET.DAT.onetime).
"""
  outfile.write(help_str)


def usage(outfile=sys.stdout):
  """Print usage information to OUTFILE."""
  usage_str = """\
OneTime version %s, an open source encryption program that uses one-time pads.

Typical usage:

  onetime -e -p PAD MSG           (encrypt; write output to 'MSG.onetime')
  onetime -d -p PAD MSG.onetime   (decrypt; output loses '.onetime' suffix)

Other usage modes:

  onetime [-e|-d] -p PAD -o OUTPUT INPUT  (both INPUT and OUTPUT are files)
  onetime [-e|-d] -p PAD -o - INPUT       (output goes to stdout)
  onetime [-e|-d] -p PAD                  (input from stdin, output to stdout)
  onetime [-e|-d] -p PAD -o OUTPUT        (input from stdin, output to OUTPUT)

OneTime remembers what ranges of what pad files have been used, and avoids
re-using those ranges when encrypting, by keeping records in ~/.onetime/.

All options:

   -e                      Encrypt
   -d                      Decrypt
   -p PAD | --pad=PAD      Use PAD for pad data.
   -o OUT | --output=OUT   Output to file OUT ("-" for stdout)
   --offset=N              Control the pad data start offset
   -n | --no-trace         Leave no record of pad usage in your config
   -C DIR | --config=DIR   Specify DIR (instead of ~/.onetime) as config area;
                           '-' for DIR means use no config area (implies -n)
   --show-id               Show a pad's ID; used with -p only
   --intro                 Show an introduction to OneTime and one-time pads
   -v | -V | --version     Show version information
   --license               Show full open source license information
   --pad-help              Show help on how to generate one-time pads
   -? | -h | --help        Show usage
""" % __version__
  outfile.write(usage_str)


def main():
  encrypting  = False
  decrypting  = False
  pad_file    = None
  incoming    = None
  output      = None
  output_name = None
  offset      = None
  config_area = None
  test_mode   = False
  error_exit  = False
  show_pad_id = False
  no_trace    = False

  try:
    opts, args = getopt.getopt(sys.argv[1:],
                               'edp:o:h?vVnC:',
                               [ "encrypt", "decrypt",
                                 "pad=",
                                 "output=",
                                 "offset=",
                                 "config=",
                                 "show-id",
                                 "no-trace",
                                 "test-mode",
                                 "intro", "help", "pad-help",
                                 "version", "license"])
  except getopt.GetoptError:
    sys.stderr.write("Error: problem processing options\n")
    usage(sys.stderr)
    sys.exit(1)

  for opt, value in opts:
    if opt == '--help' or opt == '-h' or opt == '-?':
      usage()
      sys.exit(0)
    if opt == '--pad-help':
      pad_generation_help()
      sys.exit(0)
    if opt == '--intro':
      print __doc__,
      sys.exit(0)
    elif opt == '--version' or opt == '-v' or opt == '-V':
      print "OneTime version %s" % __version__
      sys.exit(0)
    elif opt == '--license':
      license()
      sys.exit(0)
    elif opt == '--encrypt' or opt == '-e':
      encrypting = True
    elif opt == '--decrypt' or opt == '-d':
      decrypting = True
    elif opt == '--pad' or opt == '-p':
      pad_file = value
    elif opt == '--output' or opt == '-o':
      if value == "-":
        output = sys.stdout
      else:
        output_name = value
        output = open(output_name, "wb")
    elif opt == '--offset':
      offset = int(value)
    elif opt == '--config' or opt == '-C':
      config_area = value
    elif opt == '--show-id':
      show_pad_id = True
    elif opt == '--no-trace' or opt == '-n':
      no_trace = True
    elif opt == '--test-mode':
      # This option is not advertized and should never be used in
      # production.  It's just for running with predictable outputs
      # (by using pseudo-random numbers with a defined seed, instead
      # of striving for as much true randomness as we can muster), so
      # that the test suite can know what to expect.
      test_mode = True
    else:
      sys.stderr.write("Error: unrecognized option: '%s'\n" % opt)
      error_exit = True

  if show_pad_id:
    if encrypting or decrypting:
      sys.stderr.write("Error: cannot use --show-id with -e or -d.\n")
      error_exit = True
  elif not encrypting and not decrypting:
    sys.stderr.write("Error: must pass either '-e' or '-d'.\n")
    error_exit = True

  if encrypting and decrypting:
    sys.stderr.write("Error: cannot pass both '-e' and '-d'.\n")
    error_exit = True

  if not pad_file:
    sys.stderr.write("Error: must specify pad file with -p.\n")
    error_exit = True

  if len(args) == 0 or args[0] == "-":
    incoming = sys.stdin
    if output is None:
      # If incoming is stdin, output defaults to stdout.
      output = sys.stdout
  elif len(args) == 1:
    incoming = open(args[0], "rb")
    if output is None:
      if encrypting:
        # If plaintext input is 'FILENAME', output defaults to
        # 'FILENAME.onetime'.
        output_name = args[0] + ".onetime"
      else:
        # If ciphertext input is 'FILENAME.onetime', output defaults to
        # 'FILENAME'.  But we also look for ".otp", for compatibility
        # with older versions of this program.
        if args[0].endswith(".onetime"):
          output_name = args[0][:-8]
        elif args[0].endswith(".otp"):
          output_name = args[0][:-4]
        else:
          sys.stderr.write(
            "Error: input filename does not end with '.onetime' or '.otp'.\n")
          error_exit = True
      output = open(output_name, "wb")

  elif len(args) > 1:
    sys.stderr.write("Error: unexpected arguments: %s\n" % args[1:])
    error_exit = True

  if offset is not None and offset < PadSession._id_source_length:
      sys.stderr.write("Error: argument to --offset must be >= %d\n"
                       % PadSession._id_source_length)
      error_exit = True

  if error_exit:
    usage(sys.stderr)
    sys.exit(1)

  pad_sess = PadSession(pad_file, config_area, no_trace, test_mode)

  if show_pad_id:
    print pad_sess.id()
    print "  Note that older versions of OneTime (v1 and before) " \
      + "would have reported"
    print "  %s.  This v1 ID output may go away" \
      % pad_sess.id(format_level="original")
    print "  in a future release, so please do not depend on its presence."
    sys.exit(0)

  if offset is not None:
    pad_sess.set_offset(offset)
    offset = None  # junk it; we'll rely on pad_sess for offset from now on

  # The first line of OneTime format level "internal" is the begin line.
  # Then comes the header: a group of lines followed by a blank line.
  # Then comes the encoded body.
  # The last line indicates the end, and is distinguishable from
  #   encoded content by inspection
  onetime_begin = "-----BEGIN OneTime MESSAGE-----\n"
  old_onetime_begin = "-----BEGIN OTP MESSAGE-----\n"   # compat
  onetime_header = "%s" % onetime_begin                        \
               + "Format: %s" % Format_Level                   \
               + "  << NOTE: OneTime 1.x and older "           \
               +              "cannot read this format. >>\n"  \
               + "Pad ID: %s\n" % pad_sess.id()                     \
               + "Offset: %s\n" % pad_sess.offset()                 \
               + "\n"
  onetime_end = "-----END OneTime MESSAGE-----\n"
  old_onetime_end = "-----END OTP MESSAGE-----\n"       # compat

  # We could use pads more efficiently, by encrypting with multiple
  # discontinuous ranges to avoid the sparse-wasted-space problem.
  # However, the common case is that someone uses the same pad
  # successively with one interlocutor, and in that case there would
  # be no benefit to seeking out small unused ranges and consuming
  # them -- we'd pay a price in code complexity but the extra code
  # would in practice be rarely or never exercised.  Random numbers
  # aren't so expensive that they're worth that extra complexity.
  # However, we could add it in a future format rev if it ever looks
  # like a good idea.  Note that the discontinuous encrypted sections
  # would need to be embedded opaquely in the encoded output; it would
  # not be acceptable for there to be multiple plaintext "Offset:"
  # headers (or whatever) visible, as that would reveal something
  # about pad usage patterns and thus about past communications.

  if encrypting:
    output.write(onetime_header)
    encoder = SessionEncoder(pad_sess)
    while 1:
      string = incoming.read(8192)
      if len(string) > 0:
        result = encoder.encode(string)
        if result:
          output.write(result)
      else:
        result = encoder.finish()
        if result:
          output.write(result)
        break
    output.write("\n")
    output.write(onetime_end)

  elif decrypting:
    decoder = None # Will set to a decoder when know incoming format level.
    saw_end = None
    maybe_header_line = incoming.readline()
    if (maybe_header_line != onetime_begin
        and maybe_header_line != old_onetime_begin):
      sys.stderr.write("Error: malformed OneTime format: no begin line.\n")
      sys.exit(1)
    while maybe_header_line != "\n":
      maybe_header_line = incoming.readline()
      m = re.match("Offset: ([0-9]+)", maybe_header_line)
      if m:
        # Note we don't have to adjust the received offset here to
        # compensate for the 32-byte pad ID stretch, because the
        # recorded offset in *both* "original" and "internal" format
        # ciphertexts is an absolute offset from the true edge of the
        # pad.  The "internal" format pad ID stretch is already
        # reflected in the offset for modern files, and if an incoming
        # ciphertext is in original format, then naturally we just use
        # whatever offset it requests.
        #
        # (When the pad record is written back out, the 32 bytes for
        # the ID will be recorded as consumed no matter what, because
        # config.register() takes care of that.  But in practice, most
        # plaintexts will have already used more than that anyway, so
        # that safeguard is probably redundant here.)
        pad_sess.set_offset(int(m.group(1)))
        continue
      m = re.match("Format: ([a-zA-Z0-9-]+) ", maybe_header_line)
      if m:
        format_level = m.group(1)
        decoder = SessionDecoder(pad_sess, format_level)
        continue

    if decoder is None:
      # If we saw no format header, it must be the old, original format.
      decoder = SessionDecoder(pad_sess, "original")

    while 1:
      string = incoming.readline()
      if (string == onetime_end or string == old_onetime_end):
        saw_end = 1
        break
      if len(string) > 0:
        try:
          result = decoder.decode(string)
        except (IOError, PadSession.InnerFormat,
                SessionDecoder.DecodingError) as e:
          output.close()
          if output_name is not None:
            os.remove(output_name)
          raise e
        except EOFError:
          if string == "\n":
            # It's just the blank line between the end of the base64
            # data and the onetime_end marker.  Continue, because the
            # next thing we read should be the onetime_end marker.
            continue
          else:
            output.close()
            if output_name is not None:
              os.remove(output_name)
            raise SessionDecoder.DecodingError(
              "unexpected input: '%s'" % string)
        if result:
          output.write(result)
      else:
        break
    decoder.finish()
    if not saw_end:
      sys.stderr.write("Error: malformed OneTime format: no end line.\n")
      sys.exit(1)


if __name__ == '__main__':
  main()