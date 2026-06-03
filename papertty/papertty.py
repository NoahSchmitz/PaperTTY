#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Colin Nolan, 2020
# Jouko Strömmer, 2018
# Copyright and related rights waived via CC0
# https://creativecommons.org/publicdomain/zero/1.0/legalcode

# As you would expect, use this at your own risk! This code was created
# so you (yes, YOU!) can make it better.
#
# Requires Python 3

# display drivers - note: they are GPL licensed, unlike this file
import papertty.drivers.drivers_base as drivers_base
import papertty.drivers.drivers_partial as drivers_partial
import papertty.drivers.drivers_full as drivers_full
import papertty.drivers.drivers_color as drivers_color
import papertty.drivers.drivers_colordraw as drivers_colordraw
import papertty.drivers.driver_it8951 as driver_it8951
import papertty.drivers.drivers_4in2 as driver_4in2

# for ioctl
import fcntl
# for validating type of and access to device files
import os
# for gracefully handling signals (systemd service)
import signal
# for unpacking virtual console data
import struct
# for stdin and exit
import sys
import select
# for setting TTY size
import termios
# for sleeping
import time
# for command line usage
import click
# for drawing
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps
# for tidy driver list
from collections import OrderedDict
# for VNC
from vncdotool import api
# for reading stdin data for use with Pillow
from io import BytesIO

# resource path
RESOURCE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "resources")

class PaperTTY:
    """The main class - handles various settings and showing text on the display"""
    defaultfont = os.path.join(RESOURCE_PATH, "tom-thumb.pil")
    defaultsize = 8
    driver = None
    partial = None
    initialized = None
    font = None
    fontsize = None
    font_height = None
    font_width = None
    white = None
    black = None
    encoding = None
    spacing = 0
    vcom = None
    cursor = None
    rows = None
    cols = None
    is_truetype = None
    fontfile = None
    enable_a2 = True
    enable_1bpp = True
    mhz = None

    def __init__(self, driver, font=defaultfont, fontsize=defaultsize, partial=None, encoding='utf-8', spacing=0, cursor=None, vcom=None, enable_a2=True, enable_1bpp=True, mhz=None):
        """Create a PaperTTY with the chosen driver and settings"""
        self.driver = get_drivers()[driver]['class']()
        self.spacing = spacing
        self.fontsize = fontsize
        self.font = self.load_font(font) if font else None
        self.partial = partial
        self.white = self.driver.white
        self.black = self.driver.black
        self.encoding = encoding
        self.cursor = cursor
        self.vcom = vcom
        self.enable_a2 = enable_a2
        self.enable_1bpp = enable_1bpp
        self.mhz = mhz

    def ready(self):
        """Check that the driver is loaded and initialized"""
        return self.driver and self.initialized

    @staticmethod
    def error(msg, code=1):
        """Print error and exit"""
        print(msg)
        sys.exit(code)

    def set_tty_size(self, tty, rows, cols):
        """Set a TTY (/dev/tty*) to a certain size. Must be a real TTY that support ioctls."""
        self.rows = int(rows)
        self.cols = int(cols)
        with open(tty, 'w') as tty:
            size = struct.pack("HHHH", self.rows, self.cols, 0, 0)
            try:
                fcntl.ioctl(tty.fileno(), termios.TIOCSWINSZ, size)
            except OSError:
                print("TTY refused to resize (rows={}, cols={}), continuing anyway.".format(rows, cols))
                print("Try setting a sane size manually.")

    @staticmethod
    def band(bb, xdiv = 8, ydiv = 1):
        """Stretch a bounding box's X coordinates to be divisible by 8,
           otherwise weird artifacts occur as some bits are skipped."""
        #print("Before band: "+str(bb))
        return ( \
            int(bb[0] / xdiv) * xdiv, \
            int(bb[1] / ydiv) * ydiv, \
            int((bb[2] + xdiv - 1) / xdiv) * xdiv, \
            int((bb[3] + ydiv - 1) / ydiv) * ydiv \
        ) if bb else None

    @staticmethod
    def split(s, n):
        """Split a sequence into parts of size n"""
        return [s[begin:begin + n] for begin in range(0, len(s), n)]

    @staticmethod
    def fold(text, width=None, filter_fn=None):
        """Format a string to a specified width and/or filter it"""
        buff = text
        if width:
            buff = ''.join([r + '\n' for r in PaperTTY.split(buff, int(width))]).rstrip()
        if filter_fn:
            buff = [c for c in buff if filter_fn(c)]
        return buff

    @staticmethod
    def img_diff(img1, img2):
        """Return the bounding box of differences between two images"""
        return ImageChops.difference(img1, img2).getbbox()

    @staticmethod
    def ttydev(vcsa):
        """Return associated tty for vcsa device, ie. /dev/vcsa1 -> /dev/tty1"""
        return vcsa.replace("vcsa", "tty")
    
    def vcsudev(self, vcsa):
        """Return character width and associated vcs(u) for vcsa device,
           ie. for /dev/vcsa1, return (4, "/dev/vcsu1") if vcsu is available, or
           (1, "/dev/vcs1") if not"""
        dev = vcsa.replace("vcsa", "vcsu")
        if os.path.exists(dev):
            if isinstance(self.font, ImageFont.FreeTypeFont):
                return 4, dev
            else:
                print("Font {} doesn't support Unicode. Falling back to 8-bit encoding.".format(self.font.file))
                return 1, vcsa.replace("vcsa", "vcs")
        else:
            print("System does not have /dev/vcsu. Falling back to 8-bit encoding.")
            return 1, vcsa.replace("vcsa", "vcs")

    @staticmethod
    def valid_vcsa(vcsa):
        """Check that the vcsa device and associated terminal seem sane"""
        vcsa_kernel_major = 7
        tty_kernel_major = 4
        vcsa_range = range(128, 191)
        tty_range = range(1, 63)

        tty = PaperTTY.ttydev(vcsa)
        vs = os.stat(vcsa)
        ts = os.stat(tty)

        vcsa_major, vcsa_minor = os.major(vs.st_rdev), os.minor(vs.st_rdev)
        tty_major, tty_minor = os.major(ts.st_rdev), os.minor(ts.st_rdev)
        if not (vcsa_major == vcsa_kernel_major and vcsa_minor in vcsa_range):
            print("Not a valid vcsa device node: {} ({}/{})".format(vcsa, vcsa_major, vcsa_minor))
            return False
        read_vcsa = os.access(vcsa, os.R_OK)
        write_tty = os.access(tty, os.W_OK)
        if not read_vcsa:
            print("No read access to {} - maybe run with sudo?".format(vcsa))
            return False
        if not (tty_major == tty_kernel_major and tty_minor in tty_range):
            print("Not a valid TTY device node: {}".format(vcsa))
        if not write_tty:
            print("No write access to {} so cannot set terminal size, maybe run with sudo?".format(tty))
        return True

    @staticmethod
    def get_blocks(attr_sequence):
        """OPTIMIZATION: Helper to find contiguous blocks of True values in a sequence"""
        blocks = []
        in_block = False
        start = 0
        for idx, is_active in enumerate(attr_sequence):
            if is_active and not in_block:
                start = idx
                in_block = True
            elif not is_active and in_block:
                blocks.append((start, idx))
                in_block = False
        if in_block:
            blocks.append((start, len(attr_sequence)))
        return blocks
    
    def load_font(self, path, keep_if_not_found=False):
        """Load the PIL or TrueType font"""
        font = None
        # If no path is given, reuse existing font path. Good for resizing.
        path = path or self.fontfile
        if os.path.isfile(path):
            try:
                # first check if the font looks like a PILfont
                with open(path, 'rb') as f:
                    if f.readline() == b"PILfont\n":
                        self.is_truetype = False
                        print('Loading PIL font {}. Font size is ignored.'.format(path))
                        font = ImageFont.load(path)
                        # otherwise assume it's a TrueType font
                    else:
                        self.is_truetype = True
                        font = ImageFont.truetype(path, self.fontsize)
                    self.fontfile = path
            except IOError:
                self.error("Invalid font: '{}'".format(path))
        elif keep_if_not_found:
            print("The font '{}' could not be found, keep using old font.".format(path))
            font = self.font
        else:
            print("The font '{}' could not be found, using fallback font instead.".format(path))
            font = ImageFont.load_default()

        if font:
            self.recalculate_font(font)

        return font

    def recalculate_font(self, font):
        """Load the PIL or TrueType font"""
        
        # getlength is highly optimized in modern Pillow for fixed-width/monospace validation
        self.font_width = int(round(font.getlength('A')))
        
        # Use textbbox to get exact pixel heights
        left, top, right, bottom = font.getbbox('A')
        
        if hasattr(font, 'getmetrics'): # TrueType
            metrics_ascent, metrics_descent = font.getmetrics()
            self.spacing = int(self.spacing) if self.spacing != 'auto' else (metrics_descent - 2)
            print('Setting spacing to {}.'.format(self.spacing))
            self.font_height = metrics_ascent + metrics_descent + self.spacing
        else: # PIL bitmap font
            self.spacing = int(self.spacing) if self.spacing != 'auto' else 0
            self.font_height = (bottom - top) + self.spacing
    def init_display(self):
        """Initialize the display - call the driver's init method"""
        self.driver.init(partial=self.partial, vcom=self.vcom, enable_a2=self.enable_a2, enable_1bpp=self.enable_1bpp, mhz=self.mhz)
        self.initialized = True

    def fit(self, portrait=False):
        """Return the maximum columns and rows we can display with this font"""
        width = self.font_width
        height = self.font_height
        # hacky, subtract just a bit to avoid going over the border with small fonts
        pw = self.driver.width - 3
        ph = self.driver.height
        return int((pw if portrait else ph) / width), int((ph if portrait else pw) / height)

    def draw_line_cursor(self, pixel_x, cur_y, draw):
        width = self.font_width if isinstance(self.font_width, int) else 4
        cur_width = width - 1
        height = self.font_height
        offset = 0
        if self.cursor != 'default': 
            offset = int(self.cursor)
        start_y = (cur_y + 1) * height - 1 - offset
        draw.line((pixel_x, start_y, pixel_x + cur_width, start_y), fill=self.black)

    def draw_block_cursor(self, pixel_x, cur_y, image):
        width = self.font_width
        height = self.font_height
        upper_left = (pixel_x, cur_y * height)
        lower_right = (pixel_x + width - 1, (cur_y + 1) * height - 1)
        mask = Image.new('1', (image.width, image.height), self.black)
        draw = ImageDraw.Draw(mask)
        draw.rectangle([upper_left, lower_right], fill=self.white)
        return ImageChops.logical_xor(image, mask)

    def showtext(self, text, fill, cursor=None, portrait=False, flipx=False, flipy=False, oldimage=None, oldtext=None, oldcursor=None):
        """Draw a string on the screen"""
        if self.ready():
            
            #If partial updates are supported, run partialdraw_showtext() instead as it should be more efficient.
            if self.driver.supports_partial and self.partial:
                if oldtext is None:
                    oldtext = ""

                return self.partialdraw_showtext(text=text, fill=fill, cursor=cursor, portrait=portrait, flipx=flipx, flipy=flipy, oldimage=oldimage, oldtext=oldtext, oldcursor=oldcursor)

            # set order of h, w according to orientation
            image = Image.new('1', (self.driver.width, self.driver.height) if portrait else (
                self.driver.height, self.driver.width),
                            self.white)
            # create the Draw object and draw the text
            draw = ImageDraw.Draw(image)

            # This is a workaround for a font height bug in PIL
            # Split the text up by line and display each line individually.
            lines = text.split('\n')
            
            # Slice our attributes to match the text lines
            inv_lines = self.split(self.inverts, self.cols) if hasattr(self, 'inverts') and self.cols else []
            und_lines = self.split(self.underlines, self.cols) if hasattr(self, 'underlines') and self.cols else []   
            for i, line in enumerate(lines):
                if line:
                    y = i * self.font_height
                    draw.text((0,y), line, font=self.font, fill=fill, spacing=self.spacing)
                    
                    # Apply Underlines for hotkeys using fast block math
                    if i < len(und_lines) and '1' in und_lines[i]:
                        for start, end in PaperTTY.get_blocks(und_lines[i]):
                            start_px = int(round(self.font.getlength(line[:start]))) if hasattr(self.font, 'getlength') else start * self.font_width
                            end_px = int(round(self.font.getlength(line[:end]))) if hasattr(self.font, 'getlength') else end * self.font_width
                            draw.line((start_px, y + self.font_height - 2, end_px - 1, y + self.font_height - 2), fill=self.black, width=2)
                                
                    # Apply Reverse-Video for menus using fast block math
                    if i < len(inv_lines) and '1' in inv_lines[i]:
                        mask = Image.new('1', image.size, self.black)
                        mask_draw = ImageDraw.Draw(mask)
                        for start, end in PaperTTY.get_blocks(inv_lines[i]):
                            start_px = int(round(self.font.getlength(line[:start]))) if hasattr(self.font, 'getlength') else start * self.font_width
                            end_px = int(round(self.font.getlength(line[:end]))) if hasattr(self.font, 'getlength') else end * self.font_width
                            mask_draw.rectangle([start_px, y, end_px - 1, y + self.font_height - 1], fill=self.white)
                        
                        image = ImageChops.logical_xor(image, mask)
                        draw = ImageDraw.Draw(image) # Rebind draw object!

            # if we want a cursor, draw it
            if cursor and self.cursor:
                cursor_col = cursor[0]
                cursor_y = cursor[1]
                line_text = lines[cursor_y] if cursor_y < len(lines) else ""
                
                # Use the draw object's layout engine to perfectly match the rendered text
                if hasattr(draw, 'textlength'):
                    cursor_px = int(round(draw.textlength(line_text[:cursor_col], font=self.font)))
                else:
                    cursor_px = int(round(self.font.getlength(line_text[:cursor_col]))) if hasattr(self.font, 'getlength') else cursor_col * self.font_width
                    
                if self.cursor == 'block':
                    # Grab the exact rendering width of the character under the cursor
                    char = line_text[cursor_col:cursor_col+1] if cursor_col < len(line_text) else ' '
                    if hasattr(draw, 'textlength'):
                        char_width = int(round(draw.textlength(char, font=self.font)))
                    else:
                        char_width = int(round(self.font.getlength(char))) if hasattr(self.font, 'getlength') else self.font_width
                    
                    image = self.draw_block_cursor(cursor_px, cursor_y, image, char_width)
                else:
                    self.draw_line_cursor(cursor_px, cursor_y, draw)
            # rotate image if using landscape
            if not portrait:
                image = image.rotate(90, expand=True)
            # apply flips if desired
            if flipx:
                image = image.transpose(Image.FLIP_LEFT_RIGHT)
            if flipy:
                image = image.transpose(Image.FLIP_TOP_BOTTOM)
            # find out which part changed and draw only that on the display
            if oldimage and self.driver.supports_partial and self.partial:
                # create a bounding box of the altered region and
                # make the X coordinates divisible by 8
                if self.driver.supports_1bpp and self.driver.enable_1bpp:
                    xdiv = self.driver.align_1bpp_width
                    ydiv = self.driver.align_1bpp_height
                else:
                    xdiv = 8
                    ydiv = 1
                diff_bbox = self.band(self.img_diff(image, oldimage), xdiv=xdiv, ydiv=ydiv)
                # crop the altered region and draw it on the display
                if diff_bbox:
                    self.driver.draw(diff_bbox[0], diff_bbox[1], image.crop(diff_bbox))
            else:
                # if no previous image, draw the entire display
                self.driver.draw(0, 0, image)
            return image
        else:
            self.error("Display not ready")

    def partialdraw_showtext(self, text, fill, cursor, portrait, flipx, flipy, oldimage, oldtext, oldcursor):

        """Draw a string on the screen one line at a time.
           This function serves as an alternative to showtext() and aims to be more efficient
           by comparing string values instead of diffing images.
           It is especially fast for any drivers with self.supports_multi_draw = True.
           (supports_multi_draw is currently only supported by the IT8951 driver)
        """

        #First, grab oldtext (the text from the previous render) and text (the text from
        #the current render), then split them up based on a newline delimiter.
        #This is so we can compare the previous state of the text to the current state
        #of the text line by line and only redraw the lines which have actually changed.
        oldlines = oldtext.split('\n')
        newlines = text.split('\n')


        #Use the font height as the height for other measurements, such as row height
        height = self.font_height

        #Figure out the width and height of the panel after rotation.
        #These values are used when determining the maximum allowed size of a row of text,
        #when figuring out coordinates, and so on.
        driverHeight = self.driver.height if portrait else self.driver.width
        driverWidth = self.driver.width if portrait else self.driver.height
        
        #First, run through each row and build a list of strings to potentially draw
        changedLines = self.partialdraw_get_changed_lines(cursor, oldcursor, oldlines, newlines)


        #If this panel doesn't support multiple draws in a single refresh, then we
        #are probably better off merging the individual lines of text into a single
        #block and drawing that instead.
        #This is because of the overhead involved with each individual write to the
        #GPIO pins, so it can be faster to do one big write instead of two small writes.
        #
        #There's no strict rule of which is faster.
        #It depends on the speed of the machine (eg. rpi zero vs rpi 4b) and the speed
        #of the panel refresh.
        #
        #The value of maxRedraw should probably always be either 1 or 2.
        if not self.driver.supports_multi_draw:
            maxRedraw = 1
            blocks = self.partialdraw_get_text_blocks(changedLines)
            self.partialdraw_merge_text_blocks(blocks, maxRedraw, changedLines)
        

        #For each line in `changedLines`, figure out its coordinates and other information
        #needed for drawing.
        linesToDraw = self.partialdraw_get_lines_to_draw(changedLines, height, flipy, driverHeight)
        

        #Take those lines and turn them into actual images, performing all necessary
        #rotation, coordinate adjustments, etc.
        imagesToDraw = self.partialdraw_get_images_to_draw(linesToDraw, cursor, oldcursor, height, fill, flipx, flipy, driverWidth)


        #If oldimage is defined, update it by drawing the new frames onto it.
        #If not, create a new full-screen image.
        #In either case, don't actually draw the fullscreen image.
        #We're building it to a) return a full-screen image as the return value for
        #compatibility with other papertty functions and b) perform cropping for
        #1bpp alignment.
        if not oldimage:
            oldimage = Image.new('1', (driverWidth, driverHeight), self.white)
        

        #Array of bounded images to pass through to draw_multi if the driver
        #supports it via driver.supports_multi_draw
        imageArray = []


        #For each image we want to draw, paste the image onto the fullscreen image (oldimage).
        #Then build a bbox and band the image coordinates we required by the board
        #and bpp setting.
        #Finally, either draw the image immediately, or put it in imageArray so it can be
        #drawn in bulk.
        for arr in imagesToDraw:
            oldimage.paste(arr["image"], (arr["x"], arr["y"])) #for the return data

            diff_bbox = ( \
                arr["x"], \
                arr["y"], \
                arr["x"] + arr["image"].width, \
                arr["y"] + arr["image"].height \
            )
            if self.driver.supports_1bpp and self.driver.enable_1bpp:
                xdiv = self.driver.align_1bpp_width
                ydiv = self.driver.align_1bpp_height
            else:
                xdiv = 8
                ydiv = 1

            #If the screen is rotated, then switch the bounds around.
            #This is because we crop BEFORE rotating, so doing it here
            #with switched values saves us from needing to crop a second
            #time.
            if not portrait:
                xdiv, ydiv = ydiv, xdiv

            bbox = self.band(diff_bbox, xdiv=xdiv, ydiv=ydiv)

            croppedImage = oldimage.crop(bbox)
            x, y = bbox[0], bbox[1]

            #Rotate the image and coordinates
            if not portrait:
                croppedImage = croppedImage.rotate(90, expand=True)
                x, y = y, driverWidth - x - croppedImage.height

            #If multi_draw is supported, add the image to an array so they can
            #all be sent through at once.
            #Otherwise, just draw the image immediately.
            if self.driver.supports_multi_draw:
                imageArray.append({"x":x, "y":y, "image":croppedImage})
            else:
                self.driver.draw(x, y, croppedImage)


        if self.driver.supports_multi_draw:
            self.driver.draw_multi(imageArray)


        return oldimage
    
    def partialdraw_get_changed_lines(self, cursor, oldcursor, oldlines, newlines):

        """This function compares two strings arrays, oldlines and newlines, and
            figures out which lines of text in those arrays are different.
            It also takes cursor position into consideration when figuring out if
            the text has "changed" or not."""

        #List of lines of text which have changed
        changedLines = []
        # Split attribute strings by columns
        inv_lines = self.split(self.inverts, self.cols) if hasattr(self, 'inverts') else []
        old_inv_lines = self.split(self.old_inverts, self.cols) if hasattr(self, 'old_inverts') else []
        und_lines = self.split(self.underlines, self.cols) if hasattr(self, 'underlines') else []
        old_und_lines = self.split(self.old_underlines, self.cols) if hasattr(self, 'old_underlines') else []

        for i in range(self.rows):
            newval = newlines[i] if i < len(newlines) else ''
            oldval = oldlines[i] if i < len(oldlines) else ''
            
            new_inv = inv_lines[i] if i < len(inv_lines) else ()
            old_inv = old_inv_lines[i] if i < len(old_inv_lines) else ()
            new_und = und_lines[i] if i < len(und_lines) else ()
            old_und = old_und_lines[i] if i < len(old_und_lines) else ()

            cursorIsOnThisLine = False
            cursorWasOnThisLine = False
            cursorMovedHorizontally = False

            if cursor and self.cursor:
                if cursor[1] == i: cursorIsOnThisLine = True
            if oldcursor:
                if oldcursor[1] == i: cursorWasOnThisLine = True

            if cursorIsOnThisLine and cursorWasOnThisLine:
                if oldcursor[0] != cursor[0]:
                    cursorMovedHorizontally = True
                elif oldval == newval and old_inv == new_inv and old_und == new_und:
                    cursorIsOnThisLine = False
                    cursorWasOnThisLine = False

            # Draw if text OR colors change
            drawThisLine = cursorMovedHorizontally or cursorIsOnThisLine != cursorWasOnThisLine or oldval != newval or old_inv != new_inv or old_und != new_und

            lineToDraw = {
                "drawThisLine": drawThisLine,
                "newval": newval,
                "new_inv": new_inv,
                "new_und": new_und,
                "old_inv": old_inv,
                "old_und": old_und,
                "cursorIsOnThisLine": cursorIsOnThisLine,
                "oldval": oldval,
                "cursorWasOnThisLine": cursorWasOnThisLine
            }
            changedLines.append(lineToDraw)

        return changedLines

    def partialdraw_get_text_blocks(self, changedLines):

        """This function takes the result of partialdraw_get_changed_lines and
            groups consecutive lines together in order to create blocks of text."""

        #Array of grouped text blocks
        blocks = []

        #Used in the loop to keep track of whether the previous line was flagged for
        #drawing or not
        drawLastLine = False
        
        for i, arr in enumerate(changedLines):
            
            drawThisLine = arr["drawThisLine"]

            #If this line is to be drawn, and so was the previous line, group them
            #together in the same block.
            #If this line is to be drawn, but the previous one wasn't, then start a
            #new block instead.
            if drawThisLine:
                if drawLastLine:
                    blocks[-1]["end"] = i
                else:
                    blocks.append({"start":i, "end":i})

            drawLastLine = drawThisLine

        return blocks

    def partialdraw_merge_text_blocks(self, blocks, maxRedraw, changedLines):

        """This function takes the result of partialdraw_get_text_blocks
            and merges the text blocks together until the total number of block
            does not exceed `maxRedraw`."""

        #If the number of blocks to draw is more than we want to redraw separately
        #(`maxRedraw`), then batch them together.
        #We do this by setting `drawThisLine` to True for the lines in between separate
        #blocks.
        #This causes the "block" to be made bigger artificially by drawing lines we
        #don't need to, which in turn leverages the "append" behavior in the drawing loop.
        #
        #This sounds counter-productive, but it actually speeds things up in cases where
        #we can't perform multiple independent writes to the board in a single refresh.
        #This is because each individual write to SPI incurs overhead, and each individual
        #write also triggers a redraw by the e-ink panel which has its own delay.
        #So if we're looking to do multiple small writes, sometimes it's preferable to do one
        #bigger write instead.

        if len(blocks) > maxRedraw:

            #First, iterate through each block and figure out how big the gap is between
            #that block and the next block.
            #Then when we've found the smallest gap, merge those two blocks together.
            #Repeat this process until the number of blocks does not exceed `maxRedraw`.
            #The smallest gap is used as the criteria for merging because a "gap" between
            #blocks is a section of lines which otherwise don't need to be drawn.
            #Any of those lines we do draw is extra overhead.
            #So to minimize that extra overhead, we make a point of looking for the
            #smallest gaps.

            while len(blocks) > maxRedraw:
                smallestGap = -1
                smallestGapIndex = -1
                for i in range(len(blocks) - 1):
                    thisBlock = blocks[i]
                    nextBlock = blocks[i+1]
                    thisBlockEnd = thisBlock["end"]
                    nextBlockStart = nextBlock["start"]
                    gap = nextBlockStart - thisBlockEnd
                    if smallestGap == -1 or gap < smallestGap:
                        smallestGap = gap
                        smallestGapIndex = i
                blockToMerge = blocks.pop(smallestGapIndex+1)
                blocks[smallestGapIndex]["end"] = blockToMerge["end"]

            #Next, iterate through all of the lines of text we were going to draw
            #(or not draw) and reassess whether to draw them or not based on whether
            #they're in one of the calculated text blocks.
            #Setting drawThisLine to True means that line of text will be
            #flagged for merging in the drawing loop elsewhere in the code.

            for i, arr in enumerate(changedLines):
                for block in blocks:
                    if i >= block["start"] and i <= block["end"]:
                        changedLines[i]["drawThisLine"] = True
                        break

    def partialdraw_get_lines_to_draw(self, changedLines, height, flipy, driverHeight):

        """This function takes the result of partialdraw_get_changed_lines and
            figures out where and how to draw the line.
            This includes flagging the line of text as one which should be merged,
            figuring out which characters in the text line have actually changed,
            and other information useful for the text drawing loop."""
        
        #`append` is a flag to indicate that the current line should be merged with the
        #preceding line instead of being drawn separately.
        #This is part of a performance optimization; it's less expensive to draw a double-line
        #height image than it is to draw two single-line height images.
        append = False

        linesToDraw = []

        for i, arr in enumerate(changedLines):
            drawThisLine = arr["drawThisLine"]
            newval = arr["newval"]
            cursorIsOnThisLine = arr["cursorIsOnThisLine"]
            oldval = arr["oldval"]
            cursorWasOnThisLine = arr["cursorWasOnThisLine"]

            #Calculate the y coordinate based on the row number and font height.
            #If flipy is set, count the rows backwards, since we want to draw from
            #the "end" (which flipy moves to the start of the screen) instead.
            if flipy:

                #Calculate the gap between the last row and the edge of the screen,
                #then add it to y.
                #This is because we want the gap between the "last" row (which,
                #when flipped, becomes the first row) to be at the bottom of the screen,
                #not the top.
                offset_y = driverHeight % self.font_height
                
                #We want to count backwards from 1 row BEFORE self.rows, since that's
                #the maximum index we would actually draw at when counting forwards.
                maxRowIndex = self.rows - 1

                y = (maxRowIndex - i) * height
                y += offset_y
            else:
                y = i * height

            if not drawThisLine:

                #If the cursor hasn't moved to/from this line and the text hasn't changed,
                #then don't add this line to the `linesToDraw` array.
                #Just set append to false, since we aren't drawing this line and thus can't
                #append to it
                append = False

            else:
                firstChanged = -1
                lastChanged = 0
                oldlen = len(oldval)
                newlen = len(newval)
                smallerLen = min(oldlen, newlen)

                #Iterate through the old text value and the new text value char by char
                #in order to find the first non-matching character.
                #Or, if either string is empty, set firstChanged to 0.
                if oldlen == 0 or newlen == 0:
                    firstChanged = 0
                else:
                    for j in range(smallerLen):
                        if (oldval[j] != newval[j] or 
                            (j < len(arr["old_inv"]) and arr["old_inv"][j] != arr["new_inv"][j]) or 
                            (j < len(arr["old_und"]) and arr["old_und"][j] != arr["new_und"][j])):
                            firstChanged = j
                            break

                #firstChanged might not be set if one line completely encapsulates the other.
                #eg. if the line changed from "test" to "testing" then they are identical
                #until the last char of the old string, so firstChanged would never be set.
                #In that case, set firstChanged to be the final character of whichever line
                #is shorter.
                if firstChanged == -1:
                    firstChanged = min(oldlen, newlen) - 1

                #Next, try to find the LAST non-matching character.
                #If the line length has changed, then the last char won't match, so just
                #set it to that. Otherwise, iterate char by char again.
                if newlen != oldlen:
                    lastChanged = max(oldlen, newlen) - 1
                else:
                    for j in range(newlen):
                        if (oldval[j] != newval[j] or 
                            (j < len(arr["old_inv"]) and arr["old_inv"][j] != arr["new_inv"][j]) or 
                            (j < len(arr["old_und"]) and arr["old_und"][j] != arr["new_und"][j])):
                            lastChanged = j

                #Set the x coordinate to start at `firstChanged` since we won't draw
                #anything before that.
                x = firstChanged * self.font_width

                #`subsequentLines` is a list of lines which come after the current line,
                #but should be drawn in the same image as this line.
                #This is so we can draw consecutive altered lines into a single image and
                #minimize the number of SPI writes.
                subsequentLines = []

                lineToDraw = {
                    "x":x,
                    "y":y,
                    "newval":newval,
                    "new_inv":arr["new_inv"],
                    "new_und":arr["new_und"],
                    "cursorIsOnThisLine":cursorIsOnThisLine,
                    "subsequentLines":subsequentLines,
                    "firstChanged":firstChanged,
                    "lastChanged":lastChanged,
                    "cursorWasOnThisLine":cursorWasOnThisLine
                }

                #If append is true, that means this line and the previous line were both altered.
                #So we're going to take the current line and append it to the previous line and
                #draw them together.
                if append:

                    lastIndex = len(linesToDraw) - 1
                    linesToDraw[lastIndex]["subsequentLines"].append(lineToDraw)

                else:

                    linesToDraw.append(lineToDraw)
                    append = True

        return linesToDraw

    def partialdraw_get_images_to_draw(self, linesToDraw, cursor, oldcursor, height, fill, flipx, flipy, driverWidth):
        imagesToDraw = []
        
        for i, arr in enumerate(linesToDraw):
            chunks = [arr]
            for line in arr["subsequentLines"]:
                chunks.append(line)

            # OPTIMIZATION: Update the full width of the screen.
            # This completely eliminates ghosting on backspace/deletes and prevents right-side clipping.
            rowWidth = driverWidth
            rowHeight = height * len(chunks)

            # Draw the image
            image = self.partialdraw_build_image(rowWidth, rowHeight, chunks, height, fill, cursor)

            if flipx:
                image = image.transpose(Image.FLIP_LEFT_RIGHT)
            if flipy:
                image = image.transpose(Image.FLIP_TOP_BOTTOM)
            
            chunk = chunks[-1] if flipy else chunks[0]

            # Since we are drawing the full width, x is always 0
            x = 0
            y = chunk["y"]

            imagesToDraw.append({"x":x, "y":y, "image":image})

        return imagesToDraw

    def partialdraw_get_indexes_from_chunks(self, chunks, cursor, oldcursor):

        """Calculates the starting and ending character indexes of a text block.
            eg. If chunk[0] only changes from characters 0-4, but chunk[1] changed
            from characters 3-6, then we would want to know the first changed
            character (0) and last changed character (6)."""
        
        smallestStartIndex = -1
        biggestEndIndex = 0

        for chunk in chunks:
            startIndex = chunk["firstChanged"]
            endIndex = chunk["lastChanged"]

            #Don't bother checking lines where nothing has changed.
            #This could be because the line is part of a block update.
            #So it hasn't changed, but needs to be redrawn anyway.
            if startIndex == endIndex:
                pass
            if startIndex < smallestStartIndex or smallestStartIndex == -1:
                smallestStartIndex = startIndex
            if endIndex > biggestEndIndex:
                biggestEndIndex = endIndex

        #If the cursor has moved, make sure it is drawn
        for chunk in chunks:
            cursorIsOnThisLine = chunk["cursorIsOnThisLine"]
            cursorWasOnThisLine = chunk["cursorWasOnThisLine"]

            #If the cursor both was and still is on this line, it may have moved horizontally.
            #Check if x coordinates match.
            #If they don't match, then the cursor has moved and needs to be redrawn.
            #In which case we should adjust the smallest/biggest index of the text
            #redraw to include the cursor.
            if cursorIsOnThisLine and cursorWasOnThisLine:
                cur_x = cursor[0]
                old_x = oldcursor[0]
                if cur_x != old_x:
                    smaller_x = min(cur_x, old_x)
                    bigger_x = max(cur_x, old_x)
                    if smallestStartIndex == -1 or smaller_x < smallestStartIndex:
                        smallestStartIndex = smaller_x
                    if bigger_x > biggestEndIndex:
                        biggestEndIndex = bigger_x

            #If the cursor is now on a different line than it was before, and it is/was on this
            #particular line, then we need to make sure the draw includes the index where the
            #cursor is/was.
            if cursorIsOnThisLine != cursorWasOnThisLine:
                if cursorIsOnThisLine:
                    cur_x = cursor[0]
                else:
                    cur_x = oldcursor[0]
                if smallestStartIndex == -1 or cur_x < smallestStartIndex:
                    smallestStartIndex = cur_x
                if cur_x > biggestEndIndex:
                    biggestEndIndex = cur_x

        return (smallestStartIndex, biggestEndIndex)

    def partialdraw_build_image(self, rowWidth, rowHeight, chunks, height, fill, cursor):
        """Builds an image based on the chunks of text and size parameters passed in.
            Also draws the cursor, if needed."""

        image = Image.new('1', (rowWidth, rowHeight), self.white)
        draw = ImageDraw.Draw(image)

        for j, chunk in enumerate(chunks):
            x = 0
            y = j * height
            newval = chunk["newval"]
            new_inv = chunk.get("new_inv", ())
            new_und = chunk.get("new_und", ())
            # FAST Native rendering
            draw.text((x,y), newval, font=self.font, fill=fill, spacing=self.spacing)
            
            # Draw Underlines for colored/bold shortcut keys
            if True in new_und:
                for start, end in PaperTTY.get_blocks(new_und):
                    start_px = int(round(self.font.getlength(newval[:start]))) if hasattr(self.font, 'getlength') else start * self.font_width
                    end_px = int(round(self.font.getlength(newval[:end]))) if hasattr(self.font, 'getlength') else end * self.font_width
                    draw.line((start_px, y + height - 2, end_px - 1, y + height - 2), fill=self.black, width=2)

            # Invert cells with background colors (menus and tabs)
            if True in new_inv:
                mask = Image.new('1', image.size, self.black)
                mask_draw = ImageDraw.Draw(mask)
                for start, end in PaperTTY.get_blocks(new_inv):
                    start_px = int(round(self.font.getlength(newval[:start]))) if hasattr(self.font, 'getlength') else start * self.font_width
                    end_px = int(round(self.font.getlength(newval[:end]))) if hasattr(self.font, 'getlength') else end * self.font_width
                    mask_draw.rectangle([start_px, y, end_px - 1, y + height - 1], fill=self.white)
            
                image = ImageChops.logical_xor(image, mask)
                draw = ImageDraw.Draw(image) # Crucial: Rebind draw object!
                
        # Draw the hardware cursor AFTER the XOR so it doesn't get double-inverted
        for j, chunk in enumerate(chunks):
            if chunk["cursorIsOnThisLine"]:
                cursor_col = cursor[0]
                text_before_cursor = chunk["newval"][:cursor_col]
                pixel_x = int(round(self.font.getlength(text_before_cursor))) if hasattr(self.font, 'getlength') else cursor_col * self.font_width
                
                cursor_y = j
                if self.cursor == 'block':
                    image = self.draw_block_cursor(pixel_x, cursor_y, image)
                    draw = ImageDraw.Draw(image)
                else:
                    self.draw_line_cursor(pixel_x, cursor_y, draw)
                    
        return image

    def clear(self):
        """Clears the display; set all black, then all white, or use INIT mode, if driver supports it."""
        if self.ready():
            self.driver.clear()
            print('Display reinitialized.')
        else:
            self.error("Display not ready")


class Settings:
    """A class to store CLI settings so they can be referenced in the subcommands"""
    args = {}

    def __init__(self, **kwargs):
        self.args = kwargs

    def get_init_tty(self):
        tty = PaperTTY(**self.args)
        tty.init_display()
        return tty


def get_drivers():
    """Get the list of available drivers as a dict
    Format: { '<NAME>': { 'desc': '<DESCRIPTION>', 'class': <CLASS> }, ... }"""
    driverdict = {}
    driverlist = [drivers_partial.EPD1in54, drivers_partial.EPD2in13,
                  drivers_partial.EPD2in13v2, drivers_partial.EPD2in13v4,
                  drivers_partial.EPD2in9,
                  drivers_partial.EPD2in13d, driver_4in2.EPD4in2,

                  drivers_full.EPD2in7, drivers_full.EPD3in7, drivers_full.EPD7in5,
                  drivers_color.EPD7in5b_V2, drivers_full.EPD7in5v2,

                  drivers_color.EPD4in2b, drivers_color.EPD7in5b,
                  drivers_color.EPD5in83, drivers_color.EPD5in83b,
                  drivers_color.EPD5in65f,

                  drivers_colordraw.EPD1in54b, drivers_colordraw.EPD1in54c,
                  drivers_colordraw.EPD2in13b, drivers_colordraw.EPD2in7b,
                  drivers_colordraw.EPD2in9b,

                  driver_it8951.IT8951,

                  drivers_base.Dummy, drivers_base.Bitmap]
    for driver in driverlist:
        driverdict[driver.__name__] = {'desc': driver.__doc__, 'class': driver}
    return driverdict


def get_driver_list():
    """Get a neat printable driver list"""
    order = OrderedDict(sorted(get_drivers().items()))
    return '\n'.join(["{}{}".format(driver.ljust(15), order[driver]['desc']) for driver in order])


def display_image(driver, image, stretch=False, no_resize=False, fill_color="white", rotate=None, mirror=None, flip=None):
    """
    Display the given image using the given driver and options.
    :param driver: device driver (subclass of `WaveshareEPD`)
    :param image: image data to display
    :param stretch: whether to stretch the image so that it fills the screen in both dimentions
    :param no_resize: whether the image should not be resized if it does not fit the screen (will raise `RuntimeError`
    if image is too large)
    :param fill_color: colour to fill space when image is resized but one dimension does not fill the screen
    :param rotate: rotate the image by arbitrary degrees
    :param mirror: flip the image horizontally
    :param flip: flip the image vertically
    :return: the image that was rendered
    """
    if stretch and no_resize:
        raise ValueError('Cannot set "no-resize" with "stretch"')

    if mirror:
        image = ImageOps.mirror(image)
    if flip:
        image = ImageOps.flip(image)
    if rotate:
        image = image.rotate(rotate, expand=True, fillcolor=fill_color)
        
    image_width, image_height = image.size

    if stretch:
        if (image_width, image_height) == (driver.width, driver.height):
            output_image = image
        else:
            output_image = image.resize((driver.width, driver.height))
    else:
        if no_resize:
            if image_width > driver.width or image_height > driver.height:
                raise RuntimeError('Image ({0}x{1}) needs to be resized to fit the screen ({2}x{3})'
                                   .format(image_width, image_height, driver.width, driver.height))
            # Pad only
            output_image = Image.new(image.mode, (driver.width, driver.height), color=fill_color)
            output_image.paste(image, (0, 0))
        else:
            # Scales and pads
            output_image = ImageOps.pad(image, (driver.width, driver.height), color=fill_color)

    driver.draw(0, 0, output_image)

    return output_image


@click.group()
@click.option('--driver', default=None, help='Select display driver')
@click.option('--nopartial', is_flag=True, default=False, help="Don't use partial updates even if display supports it")
@click.option('--encoding', default='latin_1', help='Encoding to use for the buffer', show_default=True)
@click.pass_context
def cli(ctx, driver, nopartial, encoding):
    """Display stdin or TTY on a Waveshare e-Paper display"""
    if not driver:
        PaperTTY.error(
            "You must choose a display driver. If your 'C' variant is not listed, use the 'B' driver.\n\n{}".format(
                get_driver_list()))
    else:
        matched_drivers = [n for n in get_drivers() if n.lower() == driver.lower()]
        if not matched_drivers:
            PaperTTY.error('Invalid driver selection, choose from:\n{}'.format(get_driver_list()))
        ctx.obj = Settings(driver=matched_drivers[0], partial=not nopartial, encoding=encoding)
    pass


@click.command(name='list')
def list_drivers():
    """List available display drivers"""
    PaperTTY.error(get_driver_list(), code=0)

@click.command()
@click.option('--size', default=16, help='Stripe size to fill with (8-32)')
@click.pass_obj
def scrub(settings, size):
    """Slowly fill with black, then white"""
    if size not in range(8, 32 + 1):
        PaperTTY.error("Invalid stripe size, must be 8-32")
    ptty = settings.get_init_tty()
    ptty.driver.scrub(fillsize=size)

@click.command()
@click.option('--vcsa', default='/dev/vcsa1', help='Virtual console device (/dev/vcsa[1-63])', show_default=True)
@click.option('--font', default=PaperTTY.defaultfont, help='Path to a TrueType or PIL font', show_default=True)
@click.option('--size', 'fontsize', default=8, help='Font size', show_default=True)
@click.option('--noclear', default=False, is_flag=True, help='Leave display content on exit')
@click.option('--nocursor', default=False, is_flag=True, help="(DEPRECATED, use --cursor=none instead) Don't draw the cursor")
@click.option('--cursor', default='legacy', help='Set cursor type. Valid values are default (underscore cursor at a sensible place), block (inverts colors at cursor), none (draws no cursor) or a number n (underscore cursor n pixels from the bottom)', show_default=False)
@click.option('--sleep', default=0.1, help='Minimum sleep between refreshes', show_default=True)
@click.option('--rows', 'ttyrows', default=None, help='Set TTY rows (--cols required too)')
@click.option('--cols', 'ttycols', default=None, help='Set TTY columns (--rows required too)')
@click.option('--portrait', default=False, is_flag=True, help='Use portrait orientation', show_default=False)
@click.option('--flipx', default=False, is_flag=True, help='Flip X axis (EXPERIMENTAL/BROKEN)', show_default=False)
@click.option('--flipy', default=False, is_flag=True, help='Flip Y axis (EXPERIMENTAL/BROKEN)', show_default=False)
@click.option('--spacing', default='0', help='Line spacing for the text, "auto" to automatically determine a good value', show_default=True)
@click.option('--scrub', 'apply_scrub', is_flag=True, default=False, help='Apply scrub when starting up',
              show_default=True)
@click.option('--autofit', is_flag=True, default=False, help='Autofit terminal size to font size', show_default=True)
@click.option('--attributes', is_flag=True, default=False, help='Use attributes', show_default=True)
@click.option('--interactive', is_flag=True, default=False, help='Interactive mode')
@click.option('--vcom', default=None, help='VCOM as positive value x 1000. eg. 1460 = -1.46V')
@click.option('--disable_a2', is_flag=True, default=False, help='Disable fast A2 panel refresh for black and white images')
@click.option('--disable_1bpp', is_flag=True, default=False, help='Disable fast 1bpp mode')
@click.option('--mhz', default=None, help='Set SPI speed in MHz')
@click.pass_obj
def terminal(settings, vcsa, font, fontsize, noclear, nocursor, cursor, sleep, ttyrows, ttycols, portrait, flipx, flipy,
             spacing, apply_scrub, autofit, attributes, interactive, vcom, disable_a2, disable_1bpp, mhz):
    """Display virtual console on an e-Paper display, exit with Ctrl-C."""
    settings.args['font'] = font
    settings.args['fontsize'] = fontsize
    settings.args['spacing'] = spacing

    if cursor != 'legacy' and nocursor:
        print("--cursor and --nocursor can't be used together. To hide the cursor, use --cursor=none")
        sys.exit(1)

    if nocursor:
        print("--nocursor is deprecated. Use --cursor=none instead")
        settings.args['cursor'] = None

    if vcom:
        vcom = int(vcom)
        if vcom <= 0:
            print("VCOM should be a positive number. It will be converted automatically. eg. For a value of -1.46V, set VCOM to 1460")
        settings.args['vcom'] = vcom
    
    settings.args['enable_a2'] = not disable_a2
    settings.args['enable_1bpp'] = not disable_1bpp
    
    if mhz:
        mhz = float(mhz)
        if mhz < 0:
            print("SPI speed must be greater than 0")
            sys.exit(1)
        elif mhz > 1000:
            print("SPI speed is measured in MHz. It should be much lower than the value entered. Did you enter the speed in Hz by mistake?")
            sys.exit(1)
        else:
            settings.args['mhz'] = mhz

    if cursor == 'default' or cursor == 'legacy':
        settings.args['cursor'] = 'default'
    elif cursor == 'none':
        settings.args['cursor'] = None
    else:
        settings.args['cursor'] = cursor

    ptty = settings.get_init_tty()

    if apply_scrub:
        ptty.driver.scrub()
    oldbuff = ''
    oldimage = None
    oldcursor = None
    # dirty - should refactor to make this cleaner
    flags = {'scrub_requested': False, 'show_menu': False, 'clear': False}
    
    # handle SIGINT from `systemctl stop` and Ctrl-C
    def sigint_handler(sig, frame):
        if not interactive:
            print("Exiting (SIGINT)...")
            if not noclear:
                ptty.showtext(oldbuff, fill=ptty.white, **textargs)
            sys.exit(0)
        else:
             print('Showing menu, please wait ...')
             flags['show_menu'] = True

    # toggle scrub flag when SIGUSR1 received
    def sigusr1_handler(sig, frame):
        print("Scrubbing display (SIGUSR1)...")
        flags['scrub_requested'] = True

    signal.signal(signal.SIGINT, sigint_handler)
    signal.signal(signal.SIGUSR1, sigusr1_handler)

    # group the various params for readability
    textargs = {'portrait': portrait, 'flipx': flipx, 'flipy': flipy}

    if any([ttyrows, ttycols]) and not all([ttyrows, ttycols]):
        ptty.error("You must define both --rows and --cols to change terminal size.")
    if ptty.valid_vcsa(vcsa):
        if all([ttyrows, ttycols]):
            ptty.set_tty_size(ptty.ttydev(vcsa), ttyrows, ttycols)
        else:
            # if size not specified manually, see if autofit was requested
            if autofit:
                max_dim = ptty.fit(portrait)
                print("Automatic resize of TTY to {} rows, {} columns".format(max_dim[1], max_dim[0]))
                ptty.set_tty_size(ptty.ttydev(vcsa), max_dim[1], max_dim[0])
        if interactive:
            print("Started displaying {}, minimum update interval {} s, open menu with Ctrl-C".format(vcsa, sleep))
        else:
            print("Started displaying {}, minimum update interval {} s, exit with Ctrl-C".format(vcsa, sleep))
        character_width, vcsudev = ptty.vcsudev(vcsa)

        while True:
            if flags['show_menu']:
                flags['show_menu'] = False
                print()
                print('Rendering paused. Enter')
                print('    (f) to change font,')
                print('    (s) to change spacing,')
                if ptty.is_truetype:
                    print('    (h) to change font size,')
                print('    (c) to scrub,')
                print('    (i) reinitialize display,')
                print('    (r) do a full refresh,')
                print('    (x) to exit,')
                print('    anything else to continue.')
                print('Command line arguments for current settings:\n    --font {} --size {} --spacing {}'.format(ptty.fontfile, ptty.fontsize, ptty.spacing))

                ch = sys.stdin.readline().strip()
                if ch == 'x':
                    if not noclear:
                        ptty.showtext(oldbuff, fill=ptty.white, **textargs)
                    sys.exit(0)
                elif ch == 'f':
                    print('Current font: {}'.format(ptty.fontfile))
                    new_font = click.prompt('Enter new font (leave empty to abort)', default='', show_default=False)
                    if new_font:
                        ptty.spacing = spacing
                        ptty.font = ptty.load_font(new_font, keep_if_not_found=True)
                        if autofit:
                            max_dim = ptty.fit(portrait)
                            print("Automatic resize of TTY to {} rows, {} columns".format(max_dim[1], max_dim[0]))
                            ptty.set_tty_size(ptty.ttydev(vcsa), max_dim[1], max_dim[0])
                        oldbuff = None
                    else:
                        print('Font not changed')
                elif ch == 's':
                    print('Current spacing: {}'.format(ptty.spacing))
                    new_spacing = click.prompt('Enter new spacing (leave empty to abort)', default='empty', type=int, show_default=False)
                    if new_spacing != 'empty':
                        ptty.spacing = new_spacing
                        ptty.recalculate_font(ptty.font)
                        if autofit:
                            max_dim = ptty.fit(portrait)
                            print("Automatic resize of TTY to {} rows, {} columns".format(max_dim[1], max_dim[0]))
                            ptty.set_tty_size(ptty.ttydev(vcsa), max_dim[1], max_dim[0])
                        oldbuff = None
                    else:
                        print('Spacing not changed')
                elif ch == 'h' and ptty.is_truetype:
                    print('Current font size: {}'.format(ptty.fontsize))
                    new_fontsize = click.prompt('Enter new font size (leave empty to abort)', default='empty', type=int, show_default=False)
                    if new_fontsize != 'empty':
                        ptty.fontsize = new_fontsize
                        ptty.spacing = spacing
                        ptty.font = ptty.load_font(path=None)
                        if autofit:
                            max_dim = ptty.fit(portrait)
                            print("Automatic resize of TTY to {} rows, {} columns".format(max_dim[1], max_dim[0]))
                            ptty.set_tty_size(ptty.ttydev(vcsa), max_dim[1], max_dim[0])
                        oldbuff = None
                    else:
                        print('Font size not changed')
                elif ch == 'c':
                    flags['scrub_requested'] = True
                elif ch == 'i':
                    ptty.clear()
                    oldimage = None
                    oldbuff = None
                elif ch == 'r':
                    if oldimage:
                        ptty.driver.reset()
                        ptty.driver.init(partial=False, vcom=self.vcom, enable_a2=self.enable_a2, enable_1bpp=self.enable_1bpp, mhz=self.mhz)
                        ptty.driver.draw(0, 0, oldimage)
                        ptty.driver.reset()
                        ptty.driver.init(partial=ptty.partial, vcom=self.vcom, enable_a2=self.enable_a2, enable_1bpp=self.enable_1bpp, mhz=self.mhz)

            # if user or SIGUSR1 toggled the scrub flag, scrub display and start with a fresh image
            if flags['scrub_requested']:
                # ptty.driver.scrub()
                # Replacing the above with below for a faster "refresh" that I can trigger while running as a service, and hopefully avoid scrub not covering the whole vertical of the 6" HD screen
                ptty.clear()
                # clear old image and buffer and restore flag
                oldimage = None
                oldbuff = ''
                flags['scrub_requested'] = False
            
            with open(vcsa, 'rb') as f:
                with open(vcsudev, 'rb') as vcsu:
                    # read the first 4 bytes to get the console attributes
                    attributes = f.read(4)
                    rows, cols, x, y = list(map(ord, struct.unpack('cccc', attributes)))
                    
                    ptty.cols = cols
                    ptty.rows = rows
                    
                    # Read attribute bytes (every second byte in vcsa)
                    attr_bytes = f.read(rows * cols * 2)[1::2]
                    
                    # If background color > 0, flag for inversion
                    ptty.inverts = tuple((a >> 4) & 0x0F != 0 for a in attr_bytes)
                    # If foreground is not default gray (0x07) or black (0x00), flag for underline
                    ptty.underlines = tuple((a & 0x0F) not in (0x00, 0x07) for a in attr_bytes)
                    
                    if not hasattr(ptty, 'old_inverts'): ptty.old_inverts = ptty.inverts
                    if not hasattr(ptty, 'old_underlines'): ptty.old_underlines = ptty.underlines
        
                    # read from the text buffer 
                    buff = vcsu.read()
                    
                    if character_width == 4:
                        # work around weird bug
                        buff = buff.replace(b'\x20\x20\x20\x20', b'\x20\x00\x00\x00')
                    # find character under cursor (in case using a non-fixed width font)
                    char_under_cursor = buff[character_width * (y * rows + x):character_width * (y * rows + x + 1)]
                    encoding = 'utf_32' if character_width == 4 else ptty.encoding
                    cursor = (x, y, char_under_cursor.decode(encoding, 'ignore'))
                    # add newlines per column count
                    buff = ''.join([r.decode(encoding, 'replace') + '\n' for r in ptty.split(buff, cols * character_width)])
                    # do something only if content has changed or cursor was moved
                    if buff != oldbuff or cursor != oldcursor or ptty.inverts != ptty.old_inverts or ptty.underlines != ptty.old_underlines:
                        oldimage = ptty.showtext(buff, fill=ptty.black, cursor=cursor if not nocursor else None,
                                                oldimage=oldimage,
                                                oldtext=oldbuff,
                                                oldcursor=oldcursor,
                                                **textargs)
                        oldbuff = buff
                        oldcursor = cursor
                        ptty.old_inverts = ptty.inverts
                        ptty.old_underlines = ptty.underlines
                # delay before next update check
                time.sleep(float(sleep))

    
# add all the CLI commands
cli.add_command(scrub)
cli.add_command(terminal)
cli.add_command(list_drivers)


if __name__ == '__main__':
    cli()
